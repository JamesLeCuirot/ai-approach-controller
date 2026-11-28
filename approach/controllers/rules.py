"""The rules controller: the hand-written baseline every learned controller must beat.

It follows the stage 0 spec (docs/stage0-gatwick-spec.md):

1. Sequence: landing order by each aircraft's earliest possible landing time when
   it is released (a go-around goes to the front); each slot is the slot ahead
   plus the larger of the spacing target and the wake minimum, plus a margin (as
   time at final approach speed). Departures aren't modelled yet, so the target is
   the 3 NM radar minimum rather than Gatwick's 6 NM (one departure between
   arrivals) or 8 NM (two).
2. Separate on the way in: each new arrival gets its own altitude (4,000 ft, then
   5,000, then 6,000) until it turns base.
3. Vector tactically, with no fixed pattern: a downwind point D south of the
   centreline, a base point B, then a 30 degree intercept onto the localiser at
   the join point J. Stretching the downwind (moving D, B and J further out) is
   how it absorbs delay to make the slot.
4. Speeds: 220 kt downwind, 180 kt on base, 160 kt when cleared (the pilot slows
   to its approach speed by 4 DME). Descent to 3,000 ft on base.
5. Hand over: once cleared for the ILS the pilot flies the approach; the
   controller re-sequences any go-around.
6. Stay inside the RMA: outside it, an arrival goes direct to its hold (WILLO or
   TIMBA) and descends in the stack. It is vectored off the hold only when its slot
   can be made with a downwind that stays inside the RMA; until then it holds.
7. Stay south of the extended centreline, away from the departure routes to the north:
   every downwind is on the south side.

Stage 1a options. With ``fixed_paths`` every arrival joins final at the same distance
(no stretching), so speed is the only way to make the gaps. ``speeds`` chooses who sets
speeds: "rules" (the stage 0 speeds above), "slot" (the speed-only competitor: every
10 s it gives each aircraft the speed that makes its landing slot, from the same speeds and
bands as the AI, in approach.controllers.speed) or "none" (the AI sets them). With fixed
paths the landing slots are planned about 4.5 NM apart (FIXED_PATH_MARGIN_NM), the real
controllers' typical spacing with no departure in between, to leave speed room to work.

Stage 2 option. With ``descent="plan"`` the controller plans height as well as time, as it must once pilots
descend at real rates (about 900 ft/min, approach.data.response) rather than BlueSky's 1,500. Each aircraft must
reach its join point a little below the glide path there (3,750 ft at 11 NM, 5,000 ft at 15 NM), so it joins
the glide path from below: it leaves the hold only if some path inside the RMA is long enough for that, its
downwind runs long enough for the height still to lose, and the descent to 3,000 ft is given as soon as the
distance left needs it, not only at the turn onto base. The distance needed is the height to lose at the
aircraft's own rate once it is descending (800 ft/min until then), at its ground speed, plus a margin.

Positions are worked in the runway frame: ``along`` (NM out from the threshold on
the extended centreline) and ``lateral`` (NM right of the centreline as seen by an
aircraft on final; negative is left).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from shapely.geometry import Point

from approach import wake
from approach.controllers import speed
from approach.airports.model import Airport
from approach.controllers.base import Controller
from approach.geo import bearing_xy, distance_xy, heading_difference
from approach.instructions import Instruction

D_JOIN_MIN_NM = 11.0
D_JOIN_MAX_NM = 30.0
D_JOIN_FIXED_NM = 11.0  # stage 1a: every arrival joins final here
SPEED_ABSORB_S = 90.0  # stage 1a: delay speed alone is expected to absorb, so leave the hold no earlier than this
SLOT_DEADBAND_S = 15.0  # the speed-only competitor leaves a speed alone within this of the slot
FREE_SPARE_S = 60.0  # free_speed="fast": an aircraft this far ahead of the gap behind the one before it is free
SLOW_ROOM_KT_S = 0.32  # slow_room: a slow pilot's slowing while descending (real arrivals' 10th percentile, 0.324 kt/s)
SLOW_ROOM_TO_KT = 180  # slow_room: the speed to be down to by the join point (the top of the base and final bands)
SLOW_ROOM_MARGIN_NM = 1.0
# final_room: on the intercept and final, a speed no faster than one the slowest simulated pilot could slow from to
# FINAL_ROOM_TO_KT by FINAL_ROOM_AT_NM, where pilots begin slowing to their approach speed, some very slowly (the
# 10th percentile of real 140-kt types, 0.075 kt/s between 5 and 3 NM). FINAL_ROOM_KT_S is the slowest draw of
# approach.sim.world.draw from real arrivals' slowing while descending (10th percentile 0.324 kt/s, the tail 0.277).
FINAL_ROOM_KT_S = 0.277
FINAL_ROOM_TO_KT = 160
FINAL_ROOM_AT_NM = 5.0
FINAL_ROOM_MARGIN_NM = 0.5
INTERCEPT_DEG = 30.0
INTERCEPT_LEG_NM = 3.0
DOWNWIND_OFFSET_NM = 6.0
# eta="speeds": the time to touchdown follows the indicated airspeeds the aircraft will fly from the speed it has now
# (the standard speed of each stage, 160 kt from 7 NM and its approach speed from 5 NM), each reached at a typical
# pilot's rate of slowing down or speeding up (real arrivals: 0.64 kt/s slowing level, 0.47 descending; 0.40
# speeding up), as true airspeeds at the heights of the path. The standard speeds, not the one it has been given:
# the speed given is chosen from this estimate, so an estimate that followed it would chase its own tail.
ETA_SLOW_KT_S, ETA_FASTER_KT_S = 0.5, 0.4
ETA_BASE_FT, ETA_FINAL_FT = 3500.0, (2800.0, 2000.0, 900.0)  # typical heights on base and on the three parts of final
# fill=True: an aircraft takes a place ahead of another only with this much to spare, and may take up to this share
# of what a released aircraft on its downwind can still lose by slowing down
FILL_MARGIN_S, FILL_ROOM_SHARE = 15.0, 0.5
DOWNWIND_OUT_DEG = 60.0  # downwind="fresh": an aircraft heading within this of the outbound course is on its downwind
# Spacing between landings. Gatwick's guide uses 6 NM to leave room for one departure between arrivals, or 8 NM
# for two. With no departures in the simulator yet, arrivals are spaced to the radar minimum (or the wake minimum if
# larger), plus a margin because the timing is not exact.
TARGET_SPACING_NM = 3.0
MIXED_MODE_SPACING_NM = 6.0  # for when departures are modelled
PLANNING_MARGIN_NM = 1.0  # plan slightly long: the final director can close a gap but not open one
# Stage 1a (fixed paths): plan gaps about 4.5 NM, the real controllers' median with no departure in between
# (data/real/egkk/landing_gaps.csv). Speed alone can't open a gap that was planned too tight.
FIXED_PATH_MARGIN_NM = 1.5
# sequence="soonest": the share of the delay an aircraft has already taken on (against the landing time first
# estimated for it) that counts in its favour when choosing who leaves the holds next, so none is passed for ever.
SOONEST_FAIRNESS = 0.25
SPEED_DOWNWIND, SPEED_BASE, SPEED_FINAL = 220, 180, 160
GS_DOWNWIND, GS_BASE = 235.0, 190.0  # rough ground speeds used for timing (no wind)
REISSUE_DEG = 8.0
REISSUE_S = 20.0
PILOT_DELAY_S = 4.5
STACKS = ("WILLO", "TIMBA")
STACK_BASE_FT = 7000  # the minimum stack level (FL70 on a standard day)
SPEED_HOLD = 220
# Holding stacks work as real controllers run them: first come, first served (an aircraft that starts holding
# joins the top of the stack and the back of its queue), the lowest aircraft leaves first, no aircraft is given a
# level until the aircraft below it nearby has left it, and an aircraft heading for a level that is taken nearby
# is climbed to its own. (Tested against other versions on the nine test sessions with the most trouble at the
# stack: ordering the stack by height, waiting for a free level when leaving it, or 4, 8 or 10 NM all did worse.)
VACATE_NM = 6.0  # an aircraft below and within this distance must have left a level before another descends into it
VACATED_FT = 950  # ... by at least this much (the scoring counts a loss under 900 ft)
EARLY_WAIT_S = 60.0  # inbound_descent="early": a wait this short or less counts as none
STACK_ZONE_NM = 10.0  # stack_check="stack": an aircraft this close to a stack's fix counts as in that stack
RMA_MARGIN_NM = {"D": 1.0, "B": 0.5}  # keep the planned downwind and base points this far inside the RMA
RELEASE_NEAR_NM = 10.0  # release_where="near" or "first_near": how far outside the RMA an aircraft may be released
RELEASE_OUTSIDE_MAX_KT = 250  # release_where="first" or "first_near": no faster than this when released outside the RMA
EXPEDITE_SETTLE_S = 30.0  # after this long expediting, a descent slower or faster than the median scales the estimate
EXPEDITE_MIN_FT = 6000.0  # expedite=True: asked for only above this (lower down, real arrivals descend no faster)
# straight_in=True: an arrival on the final approach side of the airport, near the extended centreline, is brought
# straight in: direct to a short leg along the course on its own side of the centreline, then the intercept
STRAIGHT_LAT_NM = 8.0  # at most this far either side of the extended centreline
STRAIGHT_LEG_NM = 2.0  # the leg along the course before the intercept (its "downwind")
# overshoot="rejoin": an aircraft through the final approach line is turned back onto it from the north side as soon
# as it is REJOIN_THROUGH_NM through (a controller sees it is not turning), only if
# it would catch the localiser at least REJOIN_MIN_NM out, no more than REJOIN_ABOVE_GP_FT above the glide path
# (it then comes down a little faster to catch it, as from a planned join), and land at least the spacing minimum
# ahead of the aircraft behind.
REJOIN_MIN_NM = 6.0
REJOIN_THROUGH_NM = 0.3  # turned back once this far through (else a mile through: brought round)
REJOIN_ABOVE_GP_FT = 300.0
STRAIGHT_MAX_DEG = 30.0  # the track to the start of that leg at most this far off the final course
STRAIGHT_MAX_OFF_DEG = 90.0  # and the aircraft heading no further than this from the final course
# descent="plan": height planning for pilots who descend at real rates
JOIN_ALT_FT = 3000  # the level given for the join (below the glide path there, which is 3,750 ft at 11 NM)
BELOW_GLIDE_PATH_FT = 300  # plan to reach the join point this far below the glide path, to join it from below
PLAN_DESCENT_FPM = 800.0  # assumed until the aircraft is seen descending (real arrivals: median 900, slowest tenth 630)
DESCENT_MARGIN_NM = 2.0  # time to start the descent, and a little to spare
DESCENT_MARGIN_S = 35.0  # the same, as time (height_by="time"): about 2 NM at a downwind ground speed
# release_check="path": the way out of a stack, flown ahead before an aircraft is released
EXIT_PROBE_S = 240.0
EXIT_STEP_S = 10.0
EXIT_H_NM = 3.5  # a little over the 3 NM minimum: aircraft in a hold are taken to stay where they are
EXIT_V_FT = 1000.0
EXIT_CLIMB_FPM = 1500.0


@dataclass
class Plan:
    cs: str
    actype: str
    key: float  # earliest landing time when first planned: fixes the order
    side: float  # -1 = left of the centreline (south for 26L), +1 = right
    stage: str = "inbound"  # inbound (to its hold, holding if need be) -> downwind -> base -> cleared
    hold_fix: str = "WILLO"
    holding: bool = False
    stack_level: int = 0
    below_stack: bool = False  # came back below the stack (a go-around) or entered below it: kept out of the stack
    direct_sent: bool = False
    d_join: float = D_JOIN_MIN_NM
    level: int = 4000
    landing_t: float = 0.0
    hdg_cmd: float | None = None
    hdg_t: float = -1e9
    alt_cmd: float | None = None
    spd_cmd: float | None = None
    retry_t: float = -1e9  # after a blocked altitude, wait before asking again
    slot_t: float = 0.0  # the landing time that keeps the gap behind the aircraft ahead
    spare_s: float = 0.0  # how much later than the gap behind the aircraft ahead it would land (none ahead: 1e9)
    wait_s: float = 0.0  # inbound: how much later its slot is than the soonest it could land (none: negative)
    early: int = 0  # inbound_descent="early": the level to descend to on the way in, below the stack (0: none)
    warned: bool = False
    ai_turn_d: float = 0.0  # base_turns="ai": the final length the AI chose by turning base (0: not yet)
    turn_now: bool = False  # ... and the turn is still to be given
    ai_owns_turn: bool = False  # the AI said "not yet" as the planner was about to turn it: the turn is the AI's
    want_expedite: bool = False  # expedite=True: its path is planned on an expedited descent
    expedite_said: bool = False
    straight: bool = False  # straight_in=True: brought straight in, not by way of its stack
    expedite_t: float = -1e9  # when last told to expedite
    resequenced: bool = False  # brought round again after missing the localiser, until it is established
    rejoin_side: float = 0.0  # overshoot="rejoin": turned back onto the localiser from this side (0: not)
    rejoin_blocked: bool = False  # ... and the turn back was blocked by the safety check
    min_join: float = 0.0  # downwind="fresh": the shortest final the look-ahead has asked for (extend_downwind)


class RulesController(Controller):
    name = "rules"

    def __init__(self, airport: Airport, runway: str, fixed_paths: bool = False, speeds: str = "rules",
                 wind_aware: bool = False, stretch: bool = False, release: str = "careful", base_turns: str = "rule",
                 descent: str = "base", proposals: bool = False, sequence: str = "arrival", height_by: str = "distance",
                 inbound_speed: str = "standard", stack_check: str = "near",
                 inbound_descent: str = "stack", lowest_first: str = "holding", release_check: str = "none",
                 free_speed: str = "standard", release_where: str = "rma", expedite: bool = False,
                 straight_in: bool = False, released_order: bool = False, slow_room: bool = False,
                 final_room: bool = False, margin_nm: float | None = None, cancel_missed: bool = False,
                 overshoot: str = "round", expedite_rate: str = "p50", downwind: str = "kept",
                 absorb_s: float | None = None, eta: str = "legs", release_absorb_s: float | None = None,
                 release_share: float | None = None, fill: bool = False):
        """``wind_aware`` times every leg with the wind at the aircraft (from stage 1b's competitor on); off, the
        timing is for still air, as in stage 0 and stage 1a, whose results it keeps.

        With fixed paths, ``stretch`` lets a downwind run longer (the base turn later) when an aircraft has more
        delay to lose than speed alone can absorb: speed first, then the path, as a controller would. ``release``
        decides when an aircraft leaves the hold: "careful" when its slot can be made on the fixed path with speed,
        "eager" when it can be made with the longest downwind and speed.

        ``base_turns`` = "ai" (stage 1b): the planner still turns each aircraft at its own point unless the AI says
        otherwise: turn_base_now() turns it now, and hold_base_turn() (as the planner is about to turn it) leaves the
        turn to the AI, the aircraft flying on until the AI turns it or it reaches the last point that keeps its
        base inside the RMA.

        ``sequence`` is the order aircraft leave the holds in: "arrival" (every result before 14 Nov 2026), first
        come first served by the landing time first estimated for each; "soonest", whichever could reach the runway
        soonest (SOONEST_FAIRNESS). On 40 training sessions with the competitor, in 82% of releases from a hold another
        holding aircraft could have landed sooner (27% by over 2 minutes), and the runway stood idle while others
        held. Either way an aircraft leaves its stack only when none is below it, and each stack's levels stay in the
        order its aircraft joined, so re-ordering the queue never moves an aircraft up or down a stack. Tried on 40
        training sessions (the planner alone), "soonest" was no better: 25.6 minutes per arrival against 24.8, 119
        losses of separation against 113, 42 go-arounds against 34, 10 wake infringements against 4 (mixing the two
        stacks' aircraft on the downwind); it stays an option, "arrival" the default. "skip" keeps first come first
        served but passes over an aircraft that can't leave for a reason of its own (an aircraft below it in its
        stack, or too high to get down in time on any path): in 128 spells of an idle runway with aircraft holding,
        the next to land was held back by an aircraft ahead of it in the queue 30% of the time and was itself too
        high 31% (40 training sessions). On the same 40 sessions it was no better either: 25.5 minutes per arrival
        against 24.8, 606 landed against 593, 112 losses of separation against 113, but 44 go-arounds against 34,
        27 gaps inside the minimum against 21, 8 wake infringements against 4. Changing who leaves the holds by a
        fixed rule moves the delay onto final, where this planner can't absorb a changed order.

        ``height_by`` is how the planner decides an aircraft can get down in time (with descent="plan"):
        "distance" (every result before 15 Nov 2026), the track miles to the join point against the miles its descent
        takes at its ground speed now; "time", the time to fly to the join point at the planned speeds in the wind
        against the time its descent takes. In a hold the ground speed now swings with the wind from leg to leg (in a
        50 kt wind, 18 NM to get down from 7,000 ft on one leg, 27 NM on the other), and on the fixed simulator
        "too high to get down in time" was in 36% of the minutes the next aircraft to land was held back too long
        (120 training sessions, the planner alone). Tried on those 120 sessions, "time" took 25.85 minutes per
        arrival (every aircraft counted, still flying or not) against 26.19, better in 71 sessions and worse in 29,
        with 311 losses of separation against 302, 83 go-arounds against 87, 61 gaps inside the minimum against 58.

        ``inbound_speed`` (with speeds="slot") is the speed on the way to the stack: "standard" (every result before
        15 Nov 2026), 220 kt; "keep", 250 kt while the aircraft's slot needs no waiting, 220 once it does. Real
        arrivals are handed over at a median 270 kt and the planner slowed every one to 220 at once: real arrivals
        that never hold fly 59.8 NM in 16.0 minutes, the planner's in the quietest hours 67.7 NM in 18.8. Tried on
        the same 120 sessions it gained nothing: 26.38 minutes per arrival against 26.19 (better in 57 sessions,
        worse in 47), and with height_by="time" 25.72 against that one's 25.85. Arriving at the stack sooner mostly
        means holding longer there.

        ``stack_check`` is which aircraft a level in a stack is checked against before one is cleared down into it:
        "near" (every result before 16 Nov 2026), those below it within VACATE_NM; "stack", every aircraft below it
        in its stack wherever it is in the hold (and any within STACK_ZONE_NM of the fix), and the level must not be
        one another aircraft in the stack is cleared to. With "near", two aircraft on opposite sides of a hold, more
        than 6 NM apart, could be at one level a minute later: of the planner's 63 losses of separation on 24
        training sessions, 36 involved an aircraft holding or on its way to a stack, most just under 3 NM apart
        within 900 ft, one of the two descending. Neither change helped (120 other training sessions, the planner
        alone with height_by="time": 24.65 minutes per arrival, every aircraft counted, 1,890 landed of 1,930, 295
        losses of separation, 147 of them at a stack, 91 go-arounds). "stack" froze the stacks behind aircraft
        still far off: 27.84 minutes, 1,700 landed, 257 losses (115 at a stack), 83 go-arounds. "zone", the same
        check only within STACK_ZONE_NM of the fix, took 25.15 minutes (worse in 40 sessions, better in 24), with
        310 losses (162 at a stack) and 96 go-arounds. "near" stays the default. "holders" is "near" plus every
        aircraft holding in its stack, wherever it is in the hold and however far off this one still is, at the
        level it is cleared to: an aircraft is not cleared down through a level a holder is kept at. Under "near",
        an aircraft the look-ahead kept high in a hold (hold_level) was still taken by the planner as the bottom of
        its stack, and the next arrival was cleared down through it while still far off (train48_sim4_slow, session
        7: 891 ft at TIMBA two minutes later, beyond the safety check's look-ahead). The planner's own holders are
        cleared to levels below those of the aircraft on their way, so on its own the planner is barely changed.

        ``inbound_descent`` is how far an aircraft on its way to a stack descends before it is released: "stack"
        (every result before 16 Nov 2026), to its stack level, 7,000 ft or above; "early", the first aircraft of its
        stack whose slot needs no wait (EARLY_WAIT_S) descends below the stack to the level the downwind would give
        it, so it can be released without holding to lose height. In quiet training hours (12 arrivals or fewer) the
        planner's aircraft that held took 22.2 minutes from handover to landing against the real flights' 16.2, those
        that didn't 16.8 against 16.2; 67% of those handed over above 12,000 ft held, against about 1 in 10 real
        ones (120 training sessions). It made no difference (120 other training sessions, height_by="time": 24.53
        minutes per arrival against 24.65, better in 37 sessions and worse in 46, 291 losses of separation against
        295, 88 go-arounds against 91): the aircraft that hold reach their fix at a median 10,200 ft in quiet hours,
        still above the stack, too high whatever level they were cleared to. Real arrivals handed over above 12,000
        ft descend faster than the simulated pilots: a median 1,024 ft/min between 7,000 and 15,500 ft and a fastest
        tenth over 1,500, against 832 between 3,500 and 7,000.

        ``lowest_first`` is which aircraft the rule "the lowest aircraft in a stack leaves first" holds back:
        "holding" (every result before 18 Nov 2026), only those already holding; "zone", also those on their way to
        the stack within STACK_ZONE_NM of its fix; "all", those on their way wherever they are. With "holding", an
        aircraft that had not yet started holding could be released above one holding below it in its stack, and
        descend through it: 2 of the planner's first 12 losses of separation on the 24 fresh training sessions, and 1
        of the look-ahead's 9 at a stack on the 120 test sessions (traced for diagnosis only).

        ``release_check`` is whether the way out of a stack is checked before an aircraft is released: "none" (every
        result before 18 Nov 2026), or "path": flown EXIT_PROBE_S ahead, straight towards its downwind point and
        descending to its downwind level, it must not come within EXIT_H_NM and EXIT_V_FT of another aircraft sooner
        than it would by staying where it is; if it would, it waits. Of the planner's first 23 losses of separation on
        the 24 fresh training sessions, 6 were an aircraft leaving a stack and descending slowly past another at or
        into the level it had left, or the one below.

        ``free_speed`` (with speeds="slot") is the speed of an aircraft on its downwind or final whose landing is at
        least FREE_SPARE_S later than the gap behind the aircraft ahead needs: "standard" (every result before 18 Nov
        2026), the standard speed for its stage; "fast", the top of its band (250 kt on the downwind, 180 on final);
        "downwind", the top of its band on the downwind only; "fast_safe", as "fast", but on the downwind only while
        it could still get down in time at that speed (the planner's height check, with the time to the join point
        shortened in proportion to the faster airspeed). The slot
        speeds only ever slowed an aircraft down, so in quiet traffic every arrival flew the standard speeds: the
        planner's arrivals that hold in neither world took 1.9 minutes longer than the same real ones, 30 kt slower
        (120 test sessions on the old simulator).

        ``release_where`` is where an aircraft on its way to its stack may be released towards its downwind: "rma"
        (every result before 19 Nov 2026), only once inside the RMA; "near", also within RELEASE_NEAR_NM outside it;
        "any", wherever it is, as Terminal Control does when it takes an arrival off its arrival route early. Outside
        the RMA it is turned only towards it (the safety check's rule). On the first live shadow hour (18 Nov 2026,
        the planner alone, re-flown) the first arrival, with nothing ahead of it, flew 5 minutes towards WILLO waiting
        to enter the RMA; across the hour, "outside the RMA" held back aircraft still on their way in for about half of
        their minutes. But releasing them earlier made things worse: those released far out took the first landing
        slots, and the aircraft nearer the runway, handed over just after, held behind them. On 96 training sessions
        (the planner alone, height_by="time"), "near" took 26.45 minutes per arrival against 26.02 (within chance)
        and "any" 27.54 (beyond chance), with no fewer losses of separation either way. "rma" stays the default.
        "first" releases one outside the RMA only when no aircraft still waiting to be released is ahead of it, so it
        takes no one's place: the runway would otherwise wait for it; "first_near", the same within RELEASE_NEAR_NM.
        "first" releases one outside the RMA only when it is no faster than RELEASE_OUTSIDE_MAX_KT, on its downwind's
        side of the final approach course and able to get down on its usual descent, not an expedited one
        (_outside_ok). Without that, on 96 training sessions (the planner alone, seed 2027), "first" took 22.20 minutes
        per arrival against 22.58 but had 36 go-arounds against 19: arrivals released at the handover, about 30 NM out
        at FL150 and 280 to 310 kt, planned on an expedited descent, were still 1,000 to 1,500 ft above the glide path
        and over 200 kt at 12 NM. With it, 21.98 minutes and 16 go-arounds. "first_near" needs no such check: within
        RELEASE_NEAR_NM of the RMA, 21.66 minutes and 17 go-arounds without it, 22.01 and 18 with it.
        In busy spells on 96 training sessions (the planner bundle alone, 20 Nov 2026), in a fifth of the landing gaps
        the aircraft that landed next was still on its way to its stack outside the RMA 10 minutes before the one ahead
        landed, and those gaps held about a third of the time the runway stood idle beyond the spacing.

        ``expedite`` (with descent="plan" and height_by="time"): an aircraft too high to get down in time on any path
        is told to expedite its descent, if that would get it down, rather than kept holding to lose height; and one
        on its downwind too high for the final its slot needs is told to expedite rather than given a longer final.
        Real arrivals short of distance descend faster than others above 7,000 ft (a median 1,664 ft/min between
        11,000 and 16,000 ft against 1,287; approach.data.response), and the simulated pilots do so once told
        (approach.sim.world.expedite_profile). Asked for only above EXPEDITE_MIN_FT. Off, as in every result before
        19 Nov 2026. On 96 training sessions (the planner alone, height_by="time") it took 25.00 minutes per arrival
        against 26.02, quicker in 66 sessions and slower in 19, with 219 losses of separation against 260 and 66
        go-arounds against 99, all three beyond chance.

        ``straight_in`` (with fixed_paths): an arrival on the final approach side of the airport, within
        STRAIGHT_LAT_NM of the extended centreline and heading roughly the way of the final, is brought straight in
        when its slot is free (no more than speed can absorb), it can get down in time (on an expedited descent if
        need be) and its intercept is inside the RMA: direct to a short leg along the course on its own side of the
        centreline (which may start outside the RMA: it is flying towards it, as on its way to its stack), then the
        intercept, joining at the shortest final that lets it get down.
        Otherwise it goes on to its stack as before, and is tried again at every replan until it reaches it. Real
        controllers bring such arrivals straight in: 3.4% of the 26L arrivals on the training days were handed over
        within 8 NM of the centreline more than 25 NM out, landing a median 12.1 minutes later, 2% of them held. On
        the first live shadow hour two of them were sent to TIMBA, 15 NM south of the final, one of them held
        there, and joined final 8.5 and 15.3 minutes after handover, against the real 2.1 and 4.2; with straight_in
        and released_order they took 11.5 to 13.0 minutes from handover to landing (5 sets of pilots), against the
        real 11.7 and 12.0. On 96 training sessions (the planner alone, with expedite, free_speed="fast" and
        released_order) it changed 22 sessions, by little: 22.66 minutes per arrival against 22.72, 153 losses of
        separation against 159, 38 go-arounds against 36, all within chance (few arrivals are handed over on the
        final line). Off, as in every result before 19 Nov 2026.

        ``expedite_rate`` (with expedite and height_by="time"): the quantile of the measured rates (real arrivals
        short of distance, never slower than the usual rate at the same quantile) a descent not yet seen expediting
        is planned on: "p50", the median, or "p75", a crew quicker than three in four. Once the aircraft has been
        seen expediting for EXPEDITE_SETTLE_S, the estimate is scaled by its own rate as before, so a slow crew is
        found out half a minute in and its path lengthened. Planned on the median, an arrival from the south-west
        landing on 08R is sent to WILLO and back west, where real controllers bring it straight onto the final and
        its crew comes down at 1,500 ft/min or more. On the 48 training sessions (seed 2028, the planner alone,
        drifting pilots), "p75" took 18.23 minutes per arrival against 18.68 (quicker in 30 sessions, slower in 12),
        with 59 losses of separation against 88, 37 go-arounds against 39 and 45 landing gaps inside the minimum
        against 60. With the look-ahead on the same sessions: no losses of separation against 2, 3 close landing gaps
        against 4, 17.8 minutes per arrival against 18.5 (train48_sim5_p75). "p50" in every result before 25 Nov
        2026.

        ``downwind`` (with stretch): "kept" (every result before 28 Nov 2026), a downwind once lengthened is never
        shortened again, so the base turn point is always ahead; "fresh", its length is decided afresh at each
        look, shorter again once its slot allows (never shorter than turning base now would give, nor than the
        look-ahead has asked for). On the 37 training sessions landing on 26L (the planner alone), aircraft whose
        downwind had been lengthened turned base with a median 31 seconds more to spare than their slot asked for
        (a quarter over two minutes): the aircraft ahead lands a median half a minute sooner than first estimated,
        and a final lengthened to lose height early on stays long after the height has gone. ``absorb_s`` is how
        much of an aircraft's delay speed is left to take before its downwind is lengthened (SPEED_ABSORB_S, 90 s,
        unless given).

        ``eta``: "legs" (every result before 28 Nov 2026), the time to touchdown at one fixed speed for each leg;
        "speeds", from the speeds the aircraft will fly (see ``_eta_speeds``). ``release_share`` (with "speeds"): an
        aircraft leaves its stack when this share of what slowing down can take on its own path covers the wait for
        its slot, rather than a flat ``release_absorb_s`` (90 s). ``fill``: an aircraft not yet released takes the
        earliest place in the landing order that delays nobody (see ``_line_place``), rather than a place behind
        every aircraft released. With all of these (downwind "fresh", absorb_s 20, eta "speeds", release_share 0.8,
        fill) on 48 training sessions: the planner alone 37 losses of separation against 81, 14 go-arounds against
        29 and 17.5 minutes per arrival against 19.0 (08R 18.5 against 21.2); with the look-ahead no losses against
        2, 1 go-around against 3 and 17.35 minutes against 18.29 (train48_sim5_director).

        ``released_order``: an aircraft released from its stack (or on its way to it) lands behind every aircraft
        already released, rather than in the order of the landing times first estimated for each (``key``). Without
        it, an aircraft that held while one with a later first estimate was released from the other stack is put
        back ahead of it once released, and if that one is already on its base or final by then, it can't fall in
        behind: on the first live shadow hour (re-flown with straight_in) EJU78WM, released from TIMBA after RYR49FR
        from WILLO, was planned ahead of it, came within 1.3 NM of it on final and went around, in 4 of 5 sets of
        pilots. On 96 training sessions (the planner alone, with expedite and
        free_speed="fast") it took 22.72 minutes per arrival against 23.74 (quicker in 56 sessions, slower in 12),
        with 159 losses of separation against 274, 36 go-arounds against 98, 19 gaps inside the minimum against 71
        and 5 wake infringements against 14, all beyond chance: most of the planner's losses away from the stacks
        and its go-arounds came from this. Off, as in every result before 19 Nov 2026.

        ``slow_room`` (with speeds="slot"): a speed on the downwind no faster than one the aircraft could still slow
        from to SLOW_ROOM_TO_KT by its join point, at a slow pilot's rate (SLOW_ROOM_KT_S) over the track miles left
        (_track_to_join), less SLOW_ROOM_MARGIN_NM, and faster than the standard speed only while it could still get
        down in time at that speed (as free_speed="fast_safe"). From 250 kt that takes about 14 NM. With free_speed="fast", an
        aircraft with nothing ahead flew its whole downwind at 250 kt and was told 180 only at its base turn; a pilot
        who slows down slowly was then still too fast at 1,000 ft and went around: on 48 training sessions (the
        look-ahead with all four changes of 18 and 19 November) 13 go-arounds against 4 without them, the single
        arrival of session 47 among them, 167 kt at 1,000 ft. Off, as in every result before 19 Nov 2026.

        ``final_room`` (with speeds="slot"): a speed on the intercept and final no faster than one the slowest
        simulated pilot could slow from to 160 kt by 5 NM out (FINAL_ROOM_*), so 180 kt only beyond about 9 NM and
        170 to 7 NM, where the pilot takes its own speeds over. With free_speed="fast" an aircraft with nothing ahead
        flew 180 kt to 7 NM; a pilot who slows slowly while descending, and slowly again from 5 NM to its approach
        speed, was then still above its approach speed + 25 kt at 1,000 ft and went around, and often again on its
        next approach, flown the same way. Off, as in every result before 20 Nov 2026.

        ``margin_nm``: the margin added to the spacing minimum when landing slots are planned, in place of
        PLANNING_MARGIN_NM (or FIXED_PATH_MARGIN_NM with fixed paths). None keeps those, as in every result before
        21 Nov 2026.

        ``cancel_missed``: an aircraft that crosses the final approach line without catching the localiser is told
        its approach clearance is cancelled as it is brought round again, and cleared again when it next turns onto
        its intercept heading. Off (as in every result before 24 Nov 2026), it kept its clearance, so it could catch
        the localiser and glide path anywhere on its way round, at a moment nothing had planned or checked: in
        training session 3 of train48_sim4_ils it did so 160 seconds after its clearance and came down from 5,000 ft
        onto an aircraft at 4,000 ft turning in behind it (2.7 NM and 862 ft).

        ``overshoot``: what to do with an aircraft that crosses the final approach line without catching the
        localiser. "round" brings it round again on its downwind, as in every result before 24 Nov 2026. "rejoin"
        turns it straight back to intercept from the north side, as a real controller would, when it would still
        catch the localiser far enough out and low enough (REJOIN_*) and land the spacing minimum ahead of the
        aircraft behind; otherwise, or if it misses again or the turn back is blocked, it is brought round. Arrivals
        are brought onto final from the south (the departure routes are to the north), so the overshoot is to the
        north, on either runway."""
        if speeds not in ("rules", "slot", "none"):
            raise ValueError(f"speeds must be rules, slot or none, not {speeds!r}")
        if release not in ("careful", "eager"):
            raise ValueError(f"release must be careful or eager, not {release!r}")
        if base_turns not in ("rule", "ai"):
            raise ValueError(f"base_turns must be rule or ai, not {base_turns!r}")
        if descent not in ("base", "plan"):
            raise ValueError(f"descent must be base or plan, not {descent!r}")
        if sequence not in ("arrival", "soonest", "skip"):
            raise ValueError(f"sequence must be arrival, soonest or skip, not {sequence!r}")
        if height_by not in ("distance", "time"):
            raise ValueError(f"height_by must be distance or time, not {height_by!r}")
        if inbound_speed not in ("standard", "keep"):
            raise ValueError(f"inbound_speed must be standard or keep, not {inbound_speed!r}")
        if stack_check not in ("near", "stack", "zone", "holders"):
            raise ValueError(f"stack_check must be near, stack, zone or holders, not {stack_check!r}")
        self.stack_check = stack_check
        if inbound_descent not in ("stack", "early"):
            raise ValueError(f"inbound_descent must be stack or early, not {inbound_descent!r}")
        self.inbound_descent = inbound_descent
        if lowest_first not in ("holding", "zone", "all"):
            raise ValueError(f"lowest_first must be holding, zone or all, not {lowest_first!r}")
        self.lowest_first = lowest_first
        if release_check not in ("none", "path"):
            raise ValueError(f"release_check must be none or path, not {release_check!r}")
        self.release_check = release_check
        if free_speed not in ("standard", "fast", "downwind", "fast_safe"):
            raise ValueError(f"free_speed must be standard, fast, downwind or fast_safe, not {free_speed!r}")
        self.free_speed = free_speed
        if release_where not in ("rma", "near", "any", "first", "first_near"):
            raise ValueError(f"release_where must be rma, near, any, first or first_near, not {release_where!r}")
        self.release_where = release_where
        self.expedite = expedite
        self.straight_in = straight_in
        self.released_order = released_order
        self.slow_room = slow_room
        self.final_room = final_room
        self.margin_nm = margin_nm
        self.cancel_missed = cancel_missed
        if overshoot not in ("round", "rejoin"):
            raise ValueError(f"overshoot must be round or rejoin, not {overshoot!r}")
        self.overshoot = overshoot
        if expedite_rate not in ("p50", "p75"):
            raise ValueError(f"expedite_rate must be p50 or p75, not {expedite_rate!r}")
        self.expedite_rate = expedite_rate
        if downwind not in ("kept", "fresh"):
            raise ValueError(f"downwind must be kept or fresh, not {downwind!r}")
        self.downwind = downwind
        self.absorb_s = SPEED_ABSORB_S if absorb_s is None else float(absorb_s)
        if eta not in ("legs", "speeds"):
            raise ValueError(f"eta must be legs or speeds, not {eta!r}")
        self.eta = eta
        self.release_absorb_s = SPEED_ABSORB_S if release_absorb_s is None else float(release_absorb_s)
        if release_share is not None and eta != "speeds":
            raise ValueError("release_share needs eta=\"speeds\"")
        self.release_share = release_share
        self.fill = fill
        self.sequence = sequence
        self.height_by = height_by
        self.inbound_speed = inbound_speed
        self.descent = descent
        # Stage 2: with ``proposals`` the controller works out every heading, level and speed as usual but doesn't
        # send them; each plan's hdg_cmd, alt_cmd and spd_cmd is then its proposal, for the AI to keep or adjust
        # (approach.ai.vector). Directs, holds and ILS clearances are still sent.
        self.proposals = proposals
        self.base_turns = base_turns
        self.fixed_paths = fixed_paths
        self.wind_aware = wind_aware
        self.stretch = stretch
        self.release = release
        self.speeds = speeds
        self.last_speed_t = -1e9
        self.airport = airport
        self.runway = airport.runways[runway]
        self.rwy = runway
        self.plans: dict[str, Plan] = {}
        self.last_plan_t = -1e9
        self.out: list[Instruction] = []
        self.notes: list[str] = []
        self.rma = airport.rma(runway)
        self._max_join: dict[float, float] = {}
        self.went_around: set[str] = set()
        self.now = 0.0
        self.south = self._south_side()

    # ---- the RMA ---------------------------------------------------------------
    def _inside(self, x: float, y: float, margin: float = 0.0) -> bool:
        if self.rma is None:
            return True
        return self.rma.contains(x, y) and (margin == 0.0 or self.rma.polygon.exterior.distance(Point(x, y)) >= margin)

    def _may_release_at(self, v, first: bool = False) -> bool:
        """Whether an aircraft on its way to its stack is somewhere it may be released (release_where). ``first``: no
        aircraft still waiting to be released is ahead of it in this replan."""
        if self.release_where == "any" or self._inside(v.x, v.y):
            return True
        if self.release_where == "first":
            return first
        near = self.rma.distance_nm(v.x, v.y) <= RELEASE_NEAR_NM
        return near and (self.release_where == "near" or (self.release_where == "first_near" and first))

    def _outside_ok(self, v, p: "Plan", reach: float) -> bool:
        """release_where "first" or "first_near": whether an aircraft still outside the RMA is slow enough and low
        enough to be released there: no faster than RELEASE_OUTSIDE_MAX_KT, already on its downwind's side of the
        extended centreline, and able to reach the join point ``reach`` out on its usual descent (at the measured rates
        by height too, with height_by="time"), not an expedited one: an aircraft expediting its descent barely slows
        down. One released north-west of the airport flew across the final approach course to its downwind, was
        turned in too close, missed the localiser and went round at 246 kt (training session 0, seed 2027)."""
        if v.cas_kt > RELEASE_OUTSIDE_MAX_KT + 5:
            return False
        if v.lateral * p.side <= 0:
            return False  # on the far side of the final approach course: its way to the downwind would cross it
        q = Plan(**{**vars(p), "want_expedite": False})
        if self.height_by != "time":
            return self._track_to_join(v, q, reach) >= self._descent_nm(v, self._join_alt(reach))
        need = max(self._descent_s(v, self._join_alt(reach)), self._descent_s(v, self._join_alt(reach), measured=True))
        return self._time_to_join(v, q, reach) >= need

    def max_join(self, side: float) -> float:
        """The longest final (join point distance) whose downwind and base stay inside the RMA on that side."""
        if side not in self._max_join:
            best = None
            d = D_JOIN_MIN_NM
            while d <= D_JOIN_MAX_NM:
                pts = self._points(Plan("probe", "A320", 0.0, side, d_join=d))
                if all(self._inside(*pts[k], margin=m) for k, m in RMA_MARGIN_NM.items()):
                    best = d
                d += 0.5
            self._max_join[side] = best if best is not None else -1.0
        return self._max_join[side]

    def _margin(self) -> float:
        if self.margin_nm is not None:
            return self.margin_nm
        return FIXED_PATH_MARGIN_NM if self.fixed_paths else PLANNING_MARGIN_NM

    def _south_side(self) -> float:
        """The side of the centreline (-1 left, +1 right, as seen on final) that lies to the south."""
        _, y_left = self.airport.centreline_point(self.rwy, 10.0, -1.0)
        _, y_right = self.airport.centreline_point(self.rwy, 10.0, 1.0)
        return -1.0 if y_left < y_right else 1.0

    def _side(self, v) -> float:
        """Always the south side: the departure routes are to the north."""
        return self.south

    # ---- geometry --------------------------------------------------------------
    def _points(self, p: Plan) -> dict[str, tuple[float, float]]:
        lead = INTERCEPT_LEG_NM * math.cos(math.radians(INTERCEPT_DEG))
        off = INTERCEPT_LEG_NM * math.sin(math.radians(INTERCEPT_DEG))
        pts = {
            "J": (p.d_join, 0.0),
            "B": (p.d_join + lead, p.side * off),
            "D": ((p.d_join + lead + STRAIGHT_LEG_NM, p.side * off) if p.straight
                  else (p.d_join + lead + 1.0, p.side * DOWNWIND_OFFSET_NM)),
        }
        return {k: self.airport.centreline_point(self.rwy, a, l) for k, (a, l) in pts.items()}

    def _intercept_heading(self, p: Plan) -> float:
        # Left of the centreline (side -1) the aircraft must head right of the course to close it.
        return (self.runway.course_true - p.side * INTERCEPT_DEG) % 360

    def _gs(self, v, speed_kt: float, track_deg: float) -> float:
        """Ground speed along a track: the airspeed itself in still-air timing, else with the wind at the aircraft."""
        if not self.wind_aware:
            return speed_kt
        b = math.radians(track_deg)
        along = v.wind_e_kt * math.sin(b) + v.wind_n_kt * math.cos(b)
        cross = v.wind_e_kt * math.cos(b) - v.wind_n_kt * math.sin(b)
        return max(60.0, math.sqrt(max(speed_kt * speed_kt - cross * cross, 100.0)) + along)

    def _final_gs(self, v) -> float:
        """Ground speed on final, for turning a gap in miles into a gap in time."""
        return self._gs(v, 150.0, self.runway.course_true)

    @staticmethod
    def _first_turn_nm(v, to_hdg: float) -> float:
        """eta="speeds": the miles lost turning from the aircraft's heading onto the way it is to go: the arc flown
        less the progress it makes, r (d - sin d). Nothing for a small turn; about 5 NM (over a minute) for an
        aircraft in a hold pointing the other way, which the straight-line distance leaves out: an aircraft released
        from WILLO as it flew away from the turn point was taken to be 39 s early, slowed down, and a minute later
        found to be 26 s late (training session 4, 08R)."""
        radius = 1.97 * (max(v.tas_kt, 120.0) / 247.0) ** 2
        delta = math.radians(abs(heading_difference(to_hdg, v.hdg)))
        return radius * (delta - math.sin(delta))

    def _eta_speeds(self, v, p: Plan, d_join: float | None = None, slowest: bool = False) -> float:
        """eta="speeds": seconds from now to touchdown on the planned path, at the speeds the aircraft will fly
        (``slowest``: at the slowest speed of each stage's band instead of its standard one)."""
        q = Plan(**{**vars(p), "d_join": d_join if d_join is not None else p.d_join})
        pts = self._points(q)
        icpt = self._intercept_heading(q)
        base_hdg = bearing_xy(*pts["D"], *pts["B"])
        course = self.runway.course_true
        kt = ({k: float(lo) for k, (lo, _) in speed.BANDS_KT.items()} if slowest
              else {k: float(x) for k, x in speed.STANDARD_KT.items()})
        given = kt.get(q.stage, kt["downwind"])  # the standard speed of its stage
        if q.stage == "inbound":
            given = kt["downwind"]  # on the way from where it is to the turn point: as on a downwind
        on_final = kt["cleared"]
        legs = []  # (distance, indicated airspeed, height for the true airspeed, track)
        if q.stage in ("downwind", "inbound"):
            to_d = bearing_xy(v.x, v.y, *pts["D"])
            low = min(v.alt_ft, 4000.0)
            legs += [(distance_xy(v.x, v.y, *pts["D"]) - self._corner_saving(to_d, base_hdg, max(v.tas_kt, 200.0))
                      + self._first_turn_nm(v, to_d), given, (v.alt_ft + low) / 2.0, to_d),
                     (distance_xy(*pts["D"], *pts["B"]) - self._corner_saving(base_hdg, icpt, GS_BASE),
                      kt["base"], min(v.alt_ft, ETA_BASE_FT), base_hdg)]
        elif q.stage == "base":
            to_b = bearing_xy(v.x, v.y, *pts["B"])
            legs.append((distance_xy(v.x, v.y, *pts["B"]) - self._corner_saving(to_b, icpt, GS_BASE)
                         + self._first_turn_nm(v, to_b), given, min(v.alt_ft, ETA_BASE_FT), to_b))
        if q.stage in ("downwind", "base", "inbound"):
            legs.append((INTERCEPT_LEG_NM, on_final, min(v.alt_ft, float(JOIN_ALT_FT) + 300.0), icpt))
            final = q.d_join
        elif q.rejoin_side and not getattr(v, "loc", True):  # turned back onto the localiser from the far side
            leg, final = self._rejoin_legs(v)
            on_final = given
            legs.append((leg, given, min(v.alt_ft, ETA_BASE_FT), (course - q.rejoin_side * INTERCEPT_DEG) % 360))
        else:
            final, on_final = max(v.along, 0.0), given
        free = speed.FINAL_FREE_NM
        legs += [(max(final - free, 0.0), on_final, min(v.alt_ft, ETA_FINAL_FT[0]), course),
                 (min(max(final - 5.0, 0.0), free - 5.0), 160.0, min(v.alt_ft, ETA_FINAL_FT[1]), course),
                 (min(final, 5.0), float(wake.approach_speed(q.actype)), min(v.alt_ft, ETA_FINAL_FT[2]), course)]
        t, cas = 0.0, float(v.cas_kt)
        for dist, target, alt, trk in legs:
            if dist <= 0:
                continue
            k = (max(1.0 - 6.8756e-6 * alt, 0.2)) ** -2.12795  # true over indicated airspeed at that height
            g1 = self._gs(v, target * k, trk)
            if abs(cas - target) < 1.0:
                t, cas = t + dist / g1 * 3600.0, target
                continue
            g0 = self._gs(v, cas * k, trk)
            change_s = abs(cas - target) / (ETA_SLOW_KT_S if target < cas else ETA_FASTER_KT_S)
            if (g0 + g1) / 2.0 * change_s / 3600.0 >= dist:  # still changing speed at the end of the leg
                a = (g1 - g0) / change_s
                took = (dist * 3600.0 / g0 if abs(a) < 1e-9
                        else (-g0 + math.sqrt(max(g0 * g0 + 2.0 * a * dist * 3600.0, 0.0))) / a)
                t, cas = t + took, cas + (target - cas) * took / change_s
            else:
                t += change_s + (dist - (g0 + g1) / 2.0 * change_s / 3600.0) / g1 * 3600.0
                cas = target
        return t

    def _eta(self, v, p: Plan, d_join: float | None = None) -> float:
        """Seconds from now to touchdown on the planned path."""
        if self.eta == "speeds":
            return self._eta_speeds(v, p, d_join)
        q = Plan(**{**vars(p), "d_join": d_join if d_join is not None else p.d_join})
        pts = self._points(q)
        legs = []  # (distance, airspeed, track)
        icpt = self._intercept_heading(q)
        base_hdg = bearing_xy(*pts["D"], *pts["B"])
        if q.stage in ("downwind", "inbound"):
            downwind_kt = max(v.tas_kt, 200.0)  # true airspeed is faster than indicated at altitude
            to_d = bearing_xy(v.x, v.y, *pts["D"])
            legs += [
                (distance_xy(v.x, v.y, *pts["D"]) - self._corner_saving(to_d, base_hdg, downwind_kt), downwind_kt, to_d),
                (distance_xy(*pts["D"], *pts["B"]) - self._corner_saving(base_hdg, icpt, GS_BASE), GS_BASE, base_hdg),
            ]
        elif q.stage == "base":
            to_b = bearing_xy(v.x, v.y, *pts["B"])
            legs.append((distance_xy(v.x, v.y, *pts["B"]) - self._corner_saving(to_b, icpt, GS_BASE), GS_BASE, to_b))
        if q.stage in ("downwind", "base", "inbound"):
            legs.append((INTERCEPT_LEG_NM, GS_BASE, icpt))
            final = q.d_join
        elif q.rejoin_side and not getattr(v, "loc", True):  # turned back onto the localiser from the far side
            leg, final = self._rejoin_legs(v)
            legs.append((leg, GS_BASE, (self.runway.course_true - q.rejoin_side * INTERCEPT_DEG) % 360))
        else:
            final = max(v.along, 0.0)
        course = self.runway.course_true
        legs += [(max(final - 4.0, 0.0), 160.0, course), (min(final, 4.0), float(wake.approach_speed(q.actype)), course)]
        return sum(d / self._gs(v, kt, trk) * 3600.0 for d, kt, trk in legs)

    @staticmethod
    def _rejoin_legs(v) -> tuple[float, float]:
        """overshoot="rejoin": roughly, the track miles to catch the localiser turned back onto it at INTERCEPT_DEG
        from where it is (half a mile more for the turn, and a mile on), and how far out it would catch it."""
        off = abs(v.lateral) + 0.5
        a = math.radians(INTERCEPT_DEG)
        return off / math.sin(a) + 1.0, max(v.along - 1.0 - off / math.tan(a), 0.0)

    def _rejoin_heading(self, t: float, p: Plan, v) -> float | None:
        """overshoot="rejoin": the heading that turns an aircraft through the final approach line on the north side
        back onto the localiser, or None to bring it round again (see __init__)."""
        if self.overshoot != "rejoin" or p.side != self.south:  # only from the north side
            return None
        leg, catch_nm = self._rejoin_legs(v)
        if catch_nm < REJOIN_MIN_NM or v.alt_ft > self.runway.glide_path_altitude_ft(catch_nm) + REJOIN_ABOVE_GP_FT:
            return None
        hdg = (self.runway.course_true + p.side * INTERCEPT_DEG) % 360
        course = self.runway.course_true
        land = t + leg / self._gs(v, GS_BASE, hdg) * 3600.0 + sum(
            d / self._gs(v, kt, course) * 3600.0
            for d, kt in ((max(catch_nm - 4.0, 0.0), 160.0), (min(catch_nm, 4.0), float(wake.approach_speed(p.actype)))))
        behind = [q for q in self.plans.values()
                  if q is not p and q.stage in ("downwind", "base", "cleared") and q.landing_t >= p.landing_t]
        if behind:
            q = min(behind, key=lambda q: q.landing_t)
            gap_nm = max(TARGET_SPACING_NM, wake.separation_nm(p.actype, q.actype))
            if q.landing_t - land < gap_nm / self._final_gs(v) * 3600.0:
                return None
        return hdg

    @staticmethod
    def _corner_saving(from_hdg: float, to_hdg: float, tas_kt: float) -> float:
        """Distance saved by turning early instead of flying to the corner: 2r tan(d/2) - r d."""
        radius = 1.97 * (max(tas_kt, 120.0) / 247.0) ** 2
        delta = math.radians(min(abs(heading_difference(to_hdg, from_hdg)), 150.0))
        return 2 * radius * math.tan(delta / 2) - radius * delta

    def _turn_lead_nm(self, v, from_hdg: float, to_hdg: float) -> float:
        radius = 1.97 * (max(v.tas_kt, 120.0) / 247.0) ** 2
        delta = abs(heading_difference(to_hdg, from_hdg))
        return radius * math.tan(math.radians(min(delta, 150.0)) / 2) + v.tas_kt / 3600.0 * PILOT_DELAY_S

    def _track_to_join(self, v, p: Plan, d_join: float | None = None) -> float:
        """Track miles from the aircraft to its join point on the planned path (to D, D to B, then the intercept)."""
        q = Plan(**{**vars(p), "d_join": d_join if d_join is not None else p.d_join})
        pts = self._points(q)
        if q.stage in ("downwind", "inbound"):
            return distance_xy(v.x, v.y, *pts["D"]) + distance_xy(*pts["D"], *pts["B"]) + INTERCEPT_LEG_NM
        if q.stage == "base":
            return distance_xy(v.x, v.y, *pts["B"]) + INTERCEPT_LEG_NM
        return 0.0

    def _join_level(self, p: Plan) -> int:
        """The level given for the join: JOIN_ALT_FT, or for one brought straight in to a longer final, the highest
        thousand feet below the height it must reach there."""
        if not p.straight:
            return JOIN_ALT_FT
        return max(JOIN_ALT_FT, int(self._join_alt(p.d_join) // 1000) * 1000)

    def _straight_plan(self, t: float, v, p: Plan, need: float) -> Plan | None:
        """straight_in: the plan that brings an aircraft on its way in straight in, or None where it can't be."""
        if v.along <= 0 or abs(v.lateral) > STRAIGHT_LAT_NM:
            return None
        if abs(heading_difference(v.hdg, self.runway.course_true)) > STRAIGHT_MAX_OFF_DEG:
            return None
        side = self.south if abs(v.lateral) < 0.1 else math.copysign(1.0, v.lateral)
        probe = Plan(**{**vars(p), "side": side, "stage": "downwind", "straight": True, "holding": False})
        lead = INTERCEPT_LEG_NM * math.cos(math.radians(INTERCEPT_DEG))
        off = INTERCEPT_LEG_NM * math.sin(math.radians(INTERCEPT_DEG))
        # the furthest join whose leg along the course starts at most STRAIGHT_MAX_DEG off the final course from here
        d_max = (v.along - lead - STRAIGHT_LEG_NM
                 - abs(v.lateral - side * off) / math.tan(math.radians(STRAIGHT_MAX_DEG)))
        ok = []
        d = D_JOIN_FIXED_NM
        while d <= min(d_max, D_JOIN_MAX_NM) + 1e-9:
            pts = self._points(Plan(**{**vars(probe), "d_join": d}))
            if all(self._inside(*pts[k], margin=RMA_MARGIN_NM.get(k, 0.0)) for k in ("B", "J")):
                ok.append(d)  # its intercept inside the RMA (before it, it flies along the final, towards it)
            d += 0.5
        if not ok:
            return None
        lo, hi = min(ok), max(ok)
        probe.d_join = lo
        if self.descent == "plan":
            probe.d_join = self._join_for_height(v, probe, lo, hi)
            if not self._height_ok(v, probe, probe.d_join):
                if not self._expedite_helps(v, probe, probe.d_join):
                    return None
                probe.want_expedite = True
        if t + self._eta(v, probe) + SPEED_ABSORB_S < need:
            return None  # its slot is later than speed alone could make it: on to its stack
        probe.level = self._join_level(probe)
        return probe

    def _join_alt(self, d_join: float) -> float:
        """The height to reach the join point at: a little below the glide path there, and no lower than the level
        given for the join."""
        return max(JOIN_ALT_FT, self.runway.glide_path_altitude_ft(d_join) - BELOW_GLIDE_PATH_FT)

    def _descent_nm(self, v, to_alt: float) -> float:
        """Track miles needed to get down to `to_alt`: at the aircraft's own rate once it is descending, at its
        ground speed, plus a margin."""
        lose = v.alt_ft - to_alt
        if lose <= 100:
            return 0.0
        rate = min(-v.vs_fpm, 1100.0) if v.vs_fpm < -300 else PLAN_DESCENT_FPM
        return lose / max(rate, 400.0) * max(v.gs_kt, 150.0) / 60.0 + DESCENT_MARGIN_NM

    def _expedite_s(self, v, to_alt: float, p: Plan | None = None) -> float:
        """Seconds to get down to `to_alt` once expediting: at the rate real arrivals short of distance reach at each
        height (never slower than the usual rate), at the quantile ``expedite_rate`` (the median unless set), plus
        the margin. Once the aircraft has been expediting
        for EXPEDITE_SETTLE_S (told by this planner), scaled by how much faster or slower than that it is seen
        descending, as _descent_s does: planned on the median, a pilot who expedites slowly was released from FL130
        and could not get down in time (19 Nov 2026)."""
        from approach.sim.world import descent_by_height, descent_rate_at, expedite_by_height

        bands, short = descent_by_height(self.airport.icao), expedite_by_height(self.airport.icao)
        if not bands or len(short) != len(bands):
            return self._descent_s(v, to_alt, measured=True)
        q = self.expedite_rate
        fast = tuple(max(b[q], e[q]) for (_, b), (_, e) in zip(bands, short))
        scale = 1.0
        if (p is not None and getattr(v, "expedite", False) and v.vs_fpm < -300
                and self.now - p.expedite_t >= EXPEDITE_SETTLE_S):
            scale = min(max(-v.vs_fpm / descent_rate_at(self.airport.icao, fast, v.alt_ft), 0.5), 2.0)
        t, alt = 0.0, v.alt_ft
        while alt > to_alt + 1.0:
            step = min(250.0, alt - to_alt)
            t += step / max(400.0, scale * descent_rate_at(self.airport.icao, fast, alt - step / 2.0)) * 60.0
            alt -= step
        return t + DESCENT_MARGIN_S

    def _descent_s(self, v, to_alt: float, measured: bool = False) -> float:
        """Seconds needed to get down to `to_alt`: at the aircraft's own rate once it is descending, plus a margin.
        With ``measured``, at the median real rate for each height on the way down (approach.sim.world
        .descent_by_height), scaled by how much faster or slower than that the aircraft is descending now."""
        lose = v.alt_ft - to_alt
        if lose <= 100:
            return 0.0
        if measured:
            from approach.sim.world import descent_by_height, descent_rate_at

            bands = descent_by_height(self.airport.icao)
            if bands:
                median = tuple(q["p50"] for _, q in bands)
                at = lambda alt: descent_rate_at(self.airport.icao, median, alt)  # noqa: E731
                scale = min(max(-v.vs_fpm / at(v.alt_ft), 0.5), 2.0) if v.vs_fpm < -300 else 1.0
                t, alt = 0.0, v.alt_ft
                while alt > to_alt + 1.0:
                    step = min(250.0, alt - to_alt)
                    t += step / max(400.0, scale * at(alt - step / 2.0)) * 60.0
                    alt -= step
                return t + DESCENT_MARGIN_S
        rate = min(-v.vs_fpm, 1100.0) if v.vs_fpm < -300 else PLAN_DESCENT_FPM
        return lose / max(rate, 400.0) * 60.0 + DESCENT_MARGIN_S

    def _time_to_join(self, v, p: Plan, d_join: float | None = None) -> float:
        """Seconds from now to the join point on the planned path, at the planned speeds in the wind (as _eta)."""
        q = Plan(**{**vars(p), "d_join": d_join if d_join is not None else p.d_join})
        pts = self._points(q)
        icpt = self._intercept_heading(q)
        if q.stage in ("downwind", "inbound"):
            to_d = bearing_xy(v.x, v.y, *pts["D"])
            legs = [(distance_xy(v.x, v.y, *pts["D"]), max(v.tas_kt, 200.0), to_d),
                    (distance_xy(*pts["D"], *pts["B"]), GS_BASE, bearing_xy(*pts["D"], *pts["B"]))]
        elif q.stage == "base":
            legs = [(distance_xy(v.x, v.y, *pts["B"]), GS_BASE, bearing_xy(v.x, v.y, *pts["B"]))]
        else:
            return 0.0
        legs.append((INTERCEPT_LEG_NM, GS_BASE, icpt))
        return sum(d / self._gs(v, kt, trk) * 3600.0 for d, kt, trk in legs)

    def _height_ok(self, v, p: Plan, d_join: float, measured: bool = False) -> bool:
        """Whether the path to a join point d_join out is long enough to get down to a little below the glide path
        (``measured``: at the real rates for each height, height_by="time" only)."""
        if self.height_by == "time":
            if p.want_expedite or getattr(v, "expedite", False):
                return self._time_to_join(v, p, d_join) >= self._expedite_s(v, self._join_alt(d_join), p)
            return self._time_to_join(v, p, d_join) >= self._descent_s(v, self._join_alt(d_join), measured)
        return self._track_to_join(v, p, d_join) >= self._descent_nm(v, self._join_alt(d_join))

    def _expedite_helps(self, v, p: Plan, d_join: float) -> bool:
        """expedite=True: whether an aircraft too high for a join point d_join out would get down in time on an
        expedited descent (above EXPEDITE_MIN_FT, height_by="time" only)."""
        if not self.expedite or self.height_by != "time" or p.want_expedite or v.alt_ft <= EXPEDITE_MIN_FT:
            return False
        return self._height_ok(v, Plan(**{**vars(p), "want_expedite": True}), d_join)

    def _maybe_expedite(self, p: Plan, v) -> None:
        """Tell an aircraft planned on an expedited descent to expedite when it is cleared at least 1,000 ft lower,
        still above EXPEDITE_MIN_FT and not expediting already (on its way in or on its downwind, not holding).

        Again whenever that holds: an expedite ends once the aircraft reaches its cleared level, and the plan still
        counts on it. Said only once (before 19 Nov 2026 evening), an arrival told to expedite, then kept higher in its
        stack by the look-ahead (a climb, which ended the expedite) and released onto its downwind from FL130 to
        FL140, was planned on an expedited descent but came down at its usual rate, too high to land: 3 of the 13
        go-arounds on 48 training sessions with all four planner changes."""
        if (p.want_expedite and p.stage in ("inbound", "downwind") and not p.holding
                and v.alt_ft > EXPEDITE_MIN_FT and v.target_alt < v.alt_ft - 1000 and not getattr(v, "expedite", False)):
            self._say(p, "expedite", None)
            p.expedite_said, p.expedite_t = True, self.now

    def _join_for_height(self, v, p: Plan, lo: float, hi: float) -> float:
        """The shortest final, between lo and hi, whose path is long enough to lose the aircraft's height (a longer
        final adds track miles and raises the glide path at the join point, so both help)."""
        if self._height_ok(v, p, lo):
            return lo
        if not self._height_ok(v, p, hi):
            return hi
        for _ in range(20):
            mid = (lo + hi) / 2
            if self._height_ok(v, p, mid):
                hi = mid
            else:
                lo = mid
        return hi

    def _snap(self, true_hdg: float) -> float:
        """Round to a 5 degree magnetic heading, as a controller would say it."""
        mag = round(self.airport.true_to_magnetic(true_hdg) / 5.0) * 5.0
        return self.airport.magnetic_to_true(mag % 360)

    # ---- sequencing ------------------------------------------------------------
    def _release_order(self, t: float, p: Plan, views: dict) -> tuple:
        """sequence="soonest": an aircraft not yet released, by the earliest it could land on the shortest path (less
        SOONEST_FAIRNESS of the delay it has already taken on, so none waits for ever), after those that could go:
        one with an aircraft below it in its stack, or that can't get down in time on any path, comes last."""
        v = views[p.cs]
        side = self._side(v)
        probe = Plan(**{**vars(p), "side": side, "stage": "downwind"})
        shortest = D_JOIN_FIXED_NM if self.fixed_paths else D_JOIN_MIN_NM
        earliest = t + self._eta(v, probe, shortest)
        blocked = self._lower_leaves_first(p, views)
        if self.descent == "plan" and not blocked:
            longest = self.max_join(side)
            blocked = longest < 0 or not self._height_ok(v, probe, max(longest, shortest))
        if self.sequence == "skip":
            return (blocked, p.key)
        return (blocked, earliest - SOONEST_FAIRNESS * max(earliest - p.key, 0.0))

    def behind_released(self, p: Plan, views: dict) -> None:
        """Order an aircraft just released behind every aircraft already released (released_order, and always for one
        brought straight in)."""
        keys = [q.key for q in self.plans.values() if q is not p and q.cs in views and q.stage != "inbound"]
        if keys:
            p.key = max(p.key, max(keys) + 1.0)

    def _straight_pass(self, t: float, views: dict) -> None:
        """straight_in: bring straight in each aircraft on its way in (not holding) that can be, in the order they were
        first planned. One brought straight in lands behind every aircraft already released and ahead of every one
        still on its way in or holding, which wait one slot more, as when a real controller brings one straight in."""
        rank = {"cleared": 0, "base": 1, "downwind": 2}
        released = sorted((q for q in self.plans.values() if q.cs in views and q.stage != "inbound"),
                          key=lambda q: (rank.get(q.stage, 3), q.key))
        last = released[-1] if released else None
        for p in sorted((q for q in self.plans.values() if q.cs in views and q.stage == "inbound" and not q.holding),
                        key=lambda q: q.key):
            v = views[p.cs]
            need = t
            if last is not None:
                gap_nm = max(TARGET_SPACING_NM, wake.separation_nm(last.actype, p.actype)) + self._margin()
                need = max(last.slot_t, last.landing_t) + gap_nm / self._final_gs(v) * 3600.0
            straight = self._straight_plan(t, v, p, need)
            if straight is None:
                continue
            p.stage, p.side, p.d_join, p.straight = "downwind", straight.side, straight.d_join, True
            p.level, p.want_expedite = straight.level, straight.want_expedite
            p.alt_cmd = None
            p.hdg_cmd = None
            self.behind_released(p, views)
            p.landing_t = t + self._eta(v, p)
            p.slot_t = max(p.landing_t, need)
            last = p
            self.notes.append(f"{p.cs} straight in, joining at {p.d_join:.1f} NM")

    def _replan(self, t: float, views: dict) -> None:
        if self.straight_in and self.fixed_paths:
            self._straight_pass(t, views)
        if self.sequence in ("soonest", "skip"):
            rank = {p.cs: self._release_order(t, p, views) for p in self.plans.values()
                    if p.cs in views and p.stage == "inbound"}
            order = sorted((p for p in self.plans.values() if p.cs in views),
                           key=lambda p: ({"cleared": 0, "base": 1, "downwind": 2}.get(p.stage, 3),
                                          rank.get(p.cs, (False, p.key))))
        else:
            order = sorted((p for p in self.plans.values() if p.cs in views),
                           key=lambda p: ({"cleared": 0, "base": 1, "downwind": 2}.get(p.stage, 3), p.key))
        prev_t, prev_type = None, None
        stacks_seen: set[str] = set()
        waiting_ahead = 0  # aircraft on their way in or holding, not released in this replan, ahead of this one
        line: list[list] = []  # fill: the landing order laid out so far, each [time, type, plan, released, room]
        placed = None  # fill: the aircraft just dealt with and its place, added to the line before the next
        for p in order:
            v = views[p.cs]
            nobody_waiting = waiting_ahead == 0
            if self.fill:
                if placed is not None:
                    self._line_add(line, views, *placed)
                place = len(line)
                if p.stage == "inbound":  # the earliest place it can take without delaying anyone
                    place = self._line_place(line, t, views, p)
                    prev_t, prev_type = (line[place - 1][0], line[place - 1][1]) if place else (None, None)
                    nobody_waiting = all(e[3] for e in line[:place])
                placed = (p, place)
            if p.stage == "inbound":
                # Off the hold only when inside the RMA and the slot can be made with a downwind inside it.
                side = self._side(v)
                longest = self.max_join(side)
                probe = Plan(**{**vars(p), "side": side, "stage": "downwind"})
                need = t
                if prev_t is not None:
                    gap_nm = max(TARGET_SPACING_NM, wake.separation_nm(prev_type, p.actype)) + self._margin()
                    need = prev_t + gap_nm / self._final_gs(v) * 3600.0
                if self.fixed_paths:
                    probe.d_join = D_JOIN_FIXED_NM
                    reach = longest if (self.stretch and self.release == "eager") else D_JOIN_FIXED_NM
                    absorb = self.release_absorb_s
                    if self.release_share is not None:  # this share of what slowing down on its own path can take
                        absorb = self.release_share * (self._eta_speeds(v, probe, reach, slowest=True)
                                                       - self._eta_speeds(v, probe, reach))
                    can_go = (longest >= D_JOIN_FIXED_NM and self._may_release_at(v, nobody_waiting)
                              and t + self._eta(v, probe, reach) + absorb >= need)
                    height_reach = longest if self.stretch else D_JOIN_FIXED_NM
                else:
                    can_go = (longest >= D_JOIN_MIN_NM and self._may_release_at(v, nobody_waiting)
                              and t + self._eta(v, probe, longest) >= need)
                    height_reach = longest
                if (can_go and self.release_where == "first" and not self._inside(v.x, v.y)
                        and not self._outside_ok(v, probe, height_reach)):
                    can_go = False  # outside the RMA: only if slow enough, low enough and on its downwind's side
                if (self.expedite and self.descent == "plan" and not p.holding and p.wait_s <= EARLY_WAIT_S
                        and not self._height_ok(v, probe, height_reach) and self._expedite_helps(v, probe, height_reach)):
                    p.want_expedite = probe.want_expedite = True  # on its way in, its slot free: down sooner
                if can_go and self.descent == "plan" and not self._height_ok(v, probe, height_reach):
                    if self._expedite_helps(v, probe, height_reach):
                        p.want_expedite = True  # down in time on an expedited descent
                    else:
                        can_go = False  # it can't get down in time on any path inside the RMA: lower in the stack first
                if can_go and self._lower_leaves_first(p, views):
                    can_go = False  # the lowest aircraft in a stack leaves first
                if can_go:
                    ahead = sum(1 for q in self.plans.values() if q.stage == "downwind" and q.cs in views)
                    level = min(6000, 4000 + 1000 * ahead)
                    if p.early:  # already below the stack: never climb it back to the downwind level
                        level = min(level, max(p.early, int(round(v.target_alt / 1000.0)) * 1000))
                    if self.release_check == "path" and self._exit_conflict(p, v, views, level, max(longest, D_JOIN_MIN_NM)):
                        can_go = False  # its way out passes too close to another aircraft: it waits
                if can_go:
                    if self.released_order:
                        self.behind_released(p, views)
                    if self.fill:  # ahead of the released aircraft it has been placed in front of
                        after = next((e[2] for e in line[place:] if e[3]), None)
                        if after is not None:
                            before = max((e[2].key for e in line[:place] if e[3] and e[2].stage == "downwind"),
                                         default=after.key - 1.0)
                            p.key = (min(before, after.key - 1e-3) + after.key) / 2.0
                    p.stage, p.side, p.holding = "downwind", side, False
                    p.level = level
                    p.alt_cmd = None
                    p.hdg_cmd = None
                else:
                    soonest = t + self._eta(v, probe, D_JOIN_MIN_NM)
                    p.landing_t = max(need, soonest)
                    p.slot_t = p.landing_t
                    p.wait_s = need - soonest
                    if self.inbound_descent == "early":
                        # The first of its stack, with no wait: down below the stack on the way in, to the level
                        # the downwind would give it now, rather than hold to lose height at the stack.
                        first = p.hold_fix not in stacks_seen
                        if (first and not p.holding and (p.early or not p.below_stack)
                                and p.wait_s <= EARLY_WAIT_S):
                            ahead = sum(1 for q in self.plans.values() if q.stage == "downwind" and q.cs in views)
                            p.early = p.early or min(6000, 4000 + 1000 * ahead)
                        stacks_seen.add(p.hold_fix)
                    prev_t, prev_type = p.landing_t, p.actype
                    waiting_ahead += 1
                    continue
            if self.fixed_paths and p.stage != "cleared":
                if not self.stretch:
                    p.d_join = D_JOIN_FIXED_NM
                elif p.stage == "downwind" and not p.straight:
                    p.d_join = p.ai_turn_d or self._stretched_join(t, v, p, prev_t, prev_type)
                    if self.descent == "plan" and not p.ai_turn_d:
                        p.d_join = self._join_for_height(v, p, p.d_join, max(p.d_join, self.max_join(p.side)))
                p.landing_t = t + self._eta(v, p)
                p.slot_t = p.landing_t
                p.spare_s = 1e9
                if prev_t is not None:
                    gap_nm = max(TARGET_SPACING_NM, wake.separation_nm(prev_type, p.actype)) + self._margin()
                    p.slot_t = max(p.landing_t, prev_t + gap_nm / self._final_gs(v) * 3600.0)
                    p.spare_s = p.landing_t - (prev_t + gap_nm / self._final_gs(v) * 3600.0)
                prev_t, prev_type = p.slot_t, p.actype
                continue
            if p.stage == "downwind" and prev_t is not None:
                gap_nm = max(TARGET_SPACING_NM, wake.separation_nm(prev_type, p.actype)) + (
                    PLANNING_MARGIN_NM if self.margin_nm is None else self.margin_nm)
                need = prev_t + gap_nm / self._final_gs(v) * 3600.0  # landing time that keeps the gap
                lo, hi = D_JOIN_MIN_NM, max(D_JOIN_MIN_NM, self.max_join(p.side))
                if t + self._eta(v, p, hi) < need:
                    p.d_join = hi
                    if not p.warned:
                        p.warned = True
                        self.notes.append(f"{p.cs} would need holding to make its slot")
                else:
                    for _ in range(30):
                        mid = (lo + hi) / 2
                        if t + self._eta(v, p, mid) < need:
                            lo = mid
                        else:
                            hi = mid
                    p.d_join = max(D_JOIN_MIN_NM, hi)
            elif p.stage == "downwind":
                p.d_join = D_JOIN_MIN_NM
            if p.stage == "downwind" and self.descent == "plan":
                if not self._height_ok(v, p, p.d_join) and self._expedite_helps(v, p, p.d_join):
                    p.want_expedite = True  # rather than a longer final than its slot needs
                p.d_join = self._join_for_height(v, p, p.d_join, max(p.d_join, self.max_join(p.side)))
            p.landing_t = t + self._eta(v, p)
            p.slot_t = p.landing_t
            prev_t, prev_type = p.landing_t, p.actype

    # ---- stage 1b: the AI's base turns ------------------------------------------
    BASE_LEAD_NM = INTERCEPT_LEG_NM * math.cos(math.radians(INTERCEPT_DEG)) + 1.0  # the base turn point, before J

    def _turn_d(self, p: Plan) -> float:
        """The final length the aircraft will fly: the AI's choice, or the longest that keeps its base in the RMA."""
        return p.ai_turn_d or max(self.max_join(p.side), D_JOIN_FIXED_NM)

    def final_if_turned_now(self, v, p: Plan) -> float:
        """The final length (NM) if the aircraft turned base now: from its position along the downwind, allowing
        for the turn itself (the rule starts its turn this far before the turn point too)."""
        pts = self._points(Plan(**{**vars(p), "d_join": D_JOIN_FIXED_NM}))
        outbound = (self.runway.course_true + 180.0) % 360.0
        return v.along - self.BASE_LEAD_NM + self._turn_lead_nm(v, outbound, bearing_xy(*pts["D"], *pts["B"]))

    def can_turn_base(self, v, p: Plan | None) -> bool:
        """An aircraft on the downwind not yet turned, far enough out for an 11 NM final (in either mode, so the
        AI sees the same picture when it copies the rule)."""
        return (p is not None and p.stage == "downwind" and not p.ai_turn_d
                and self.final_if_turned_now(v, p) >= D_JOIN_FIXED_NM)

    def planner_turns_soon(self, v, p: Plan, seconds: float) -> bool:
        """The planner would turn this aircraft within `seconds` (and the turn is still the planner's)."""
        return (not p.ai_owns_turn and not p.ai_turn_d
                and self.to_planned_turn_nm(v, p) <= v.gs_kt * seconds / 3600.0)

    def hold_base_turn(self, cs: str, v, seconds: float) -> bool:
        """The AI's "not yet": if the planner is about to turn this aircraft, the turn becomes the AI's. Returns
        whether it was taken (otherwise "not yet" is the planner's plan anyway)."""
        p = self.plans.get(cs)
        if self.base_turns != "ai" or not self.can_turn_base(v, p) or not self.planner_turns_soon(v, p, seconds):
            return False
        p.ai_owns_turn = True
        return True

    def to_planned_turn_nm(self, v, p: Plan) -> float:
        """How far the aircraft is from where the planner would turn it onto base (p.d_join, its choice until the AI
        turns the aircraft), less the distance the turn starts before that point: at or below zero, it would turn."""
        pts = self._points(p)
        to_d = bearing_xy(v.x, v.y, *pts["D"])
        return distance_xy(v.x, v.y, *pts["D"]) - self._turn_lead_nm(v, to_d, bearing_xy(*pts["D"], *pts["B"]))

    def turn_base_now(self, cs: str, v) -> bool:
        """The AI's instruction: turn this aircraft onto base now. Returns whether it was taken."""
        p = self.plans.get(cs)
        if self.base_turns != "ai" or not self.can_turn_base(v, p):
            return False
        p.ai_turn_d = min(self.final_if_turned_now(v, p), self._turn_d(p))
        p.turn_now = True  # given at the controller's next look, whatever the aircraft's distance from the line
        self.last_plan_t = -1e9  # replan with the new path
        return True

    def eta_if_turned_now(self, v, p: Plan) -> float:
        """Seconds to touchdown if the aircraft turned base now (or at the earliest point, if not yet there)."""
        d = min(max(self.final_if_turned_now(v, p), D_JOIN_FIXED_NM), self._turn_d(p))
        return self._eta(v, p, d)

    def _stretched_join(self, t: float, v, p: Plan, prev_t: float | None, prev_type: str | None) -> float:
        """The final length for an aircraft on the downwind: the fixed one, or longer if it has more delay to lose
        than speed alone can absorb. Never shorter than already planned, so the base turn point is always ahead."""
        if self.downwind == "fresh":  # decided afresh at each look: shorter again once the slot allows it
            d = max(D_JOIN_FIXED_NM, p.min_join, min(p.d_join, self._shortest_join_now(v, p)))
        else:
            d = max(p.d_join, D_JOIN_FIXED_NM)
        if prev_t is None:
            return d
        gap_nm = max(TARGET_SPACING_NM, wake.separation_nm(prev_type, p.actype)) + self._margin()
        want = prev_t + gap_nm / self._final_gs(v) * 3600.0 - self.absorb_s  # speed takes the rest
        if t + self._eta(v, p, d) >= want:
            return d
        lo, hi = d, max(d, self.max_join(p.side))
        if t + self._eta(v, p, hi) < want:
            return hi
        for _ in range(20):
            mid = (lo + hi) / 2
            if t + self._eta(v, p, mid) < want:
                lo = mid
            else:
                hi = mid
        return hi

    def _gap_s(self, lead_type: str, p: Plan, v) -> float:
        """Seconds an aircraft must land behind one of that type: the spacing and the margin, at its speed on final."""
        return ((max(TARGET_SPACING_NM, wake.separation_nm(lead_type, p.actype)) + self._margin())
                / self._final_gs(v) * 3600.0)

    def _line_place(self, line: list[list], t: float, views: dict, p: Plan) -> int:
        """fill=True: the place in the landing order laid out so far for an aircraft not yet released: the earliest
        where it can land the gap behind the one before it and the gap (and FILL_MARGIN_S) ahead of the one after,
        so that nobody is delayed, or no more than a released aircraft still on its downwind can take by slowing
        down. Never ahead of an aircraft on base or cleared for the approach, nor of one ahead of it in its own stack
        (one already holding passes one still on its way to the stack). Otherwise last.

        Without it the order is: everyone released, then the rest first come first served. An aircraft released
        from TIMBA is 30 NM from 08R's turn point, so the aircraft holding at WILLO, 14 NM from it, waited behind an
        aircraft ten minutes from landing while the runway stood empty for four (training session 17)."""
        v = views[p.cs]
        probe = Plan(**{**vars(p), "side": self._side(v), "stage": "downwind"})
        soon = t + self._eta(v, probe, D_JOIN_FIXED_NM if self.fixed_paths else D_JOIN_MIN_NM)
        start = 0
        for i, (_, _, q, released, _) in enumerate(line):
            if (released and q.stage != "downwind") or (
                    not released and q.hold_fix == p.hold_fix and (q.holding or not p.holding)):
                start = i + 1
        for i in range(start, len(line)):
            when, _, q, _, room = line[i]
            lands = soon if i == 0 else max(line[i - 1][0] + self._gap_s(line[i - 1][1], p, v), soon)
            if lands + self._gap_s(p.actype, q, views[q.cs]) + FILL_MARGIN_S <= when + room:
                return i
        return len(line)

    def _line_add(self, line: list[list], views: dict, p: Plan, place: int) -> None:
        """fill=True: put an aircraft that has been dealt with into the landing order at its place; those behind it
        move back if it takes some of their gap."""
        released = p.stage != "inbound"
        room = 0.0
        if released and p.stage == "downwind" and self.eta == "speeds" and not p.straight:
            v = views[p.cs]
            room = max(0.0, FILL_ROOM_SHARE * (self._eta_speeds(v, p, slowest=True) - self._eta_speeds(v, p))
                       - (p.slot_t - p.landing_t))
        line.insert(place, [p.slot_t if released else p.landing_t, p.actype, p, released, room])
        for j in range(place + 1, len(line)):
            push = line[j - 1][0] + self._gap_s(line[j - 1][1], line[j][2], views[line[j][2].cs]) - line[j][0]
            if push <= 0:
                break
            line[j][0] += push
            line[j][4] = max(0.0, line[j][4] - push)

    def _shortest_join_now(self, v, p: Plan) -> float:
        """downwind="fresh": the shortest final still open to an aircraft on the downwind: the one it would have if it
        turned base now, once it is flying out along the downwind (within DOWNWIND_OUT_DEG of the outbound course);
        before that, on its way to the turn point from elsewhere, the fixed one."""
        outbound = (self.runway.course_true + 180.0) % 360.0
        if abs(heading_difference(v.hdg, outbound)) > DOWNWIND_OUT_DEG:
            return D_JOIN_FIXED_NM
        return self.final_if_turned_now(v, p)

    # ---- instructions ----------------------------------------------------------
    def _say(self, p: Plan, kind: str, value) -> None:
        if not (self.proposals and kind in ("heading", "altitude", "speed")):
            self.out.append(Instruction(p.cs, kind, value))
        if kind == "heading":
            p.hdg_cmd = value
        elif kind == "altitude":
            p.alt_cmd = value
        elif kind == "speed":
            p.spd_cmd = value

    def _steer(self, t: float, p: Plan, v, target_xy, force: bool = False) -> None:
        want = self._snap(bearing_xy(v.x, v.y, *target_xy))
        if force or p.hdg_cmd is None or (
            abs(heading_difference(want, p.hdg_cmd)) > REISSUE_DEG and t - p.hdg_t > REISSUE_S
        ):
            if p.hdg_cmd is None or abs(heading_difference(want, p.hdg_cmd)) >= 2.5:
                self._say(p, "heading", want)
                p.hdg_t = t

    def decide(self, t: float, views_list: list) -> list[Instruction]:
        self.out = []
        self.now = t
        views = {v.cs: v for v in views_list}
        # New arrivals (and go-arounds back under control) get a plan.
        for v in views_list:
            if v.cs not in self.plans and v.phase == "vectoring" and not v.cleared:
                side = self._side(v)
                p = Plan(v.cs, v.actype, key=0.0, side=side)
                p.hold_fix = min(STACKS, key=lambda f: distance_xy(v.x, v.y, *self.airport.xy(self.airport.holds[f].position)))
                p.key = t + self._eta(v, Plan(**{**vars(p), "stage": "downwind"}))
                if v.cs in self.went_around:  # back to the front of the queue
                    self.went_around.discard(v.cs)
                    p.key = t
                p.below_stack = v.target_alt < STACK_BASE_FT  # stays below the stack until it is vectored off
                self.plans[v.cs] = p
                self.last_plan_t = -1e9  # replan now
        for cs in [cs for cs in self.plans if cs not in views]:
            del self.plans[cs]  # landed
        if t - self.last_plan_t >= 30:
            self._replan(t, views)
            self.last_plan_t = t

        self._stack_levels(views)
        for p in sorted(self.plans.values(), key=lambda p: p.key):
            v = views[p.cs]
            if p.stage == "cleared":
                # Crossed to the far side of final without capturing the localiser (or turned back onto it from
                # there, crossed back): turn it back onto it, or bring it round again.
                side = p.rejoin_side or p.side
                first = self.overshoot == "rejoin" and not p.rejoin_side and p.side == self.south
                if not v.loc and (v.lateral * side < -(REJOIN_THROUGH_NM if first else 1.0) or p.rejoin_blocked):
                    hdg = self._rejoin_heading(t, p, v) if not (p.rejoin_side or p.rejoin_blocked) else None
                    if hdg is not None:
                        p.rejoin_side = -p.side
                        self._say(p, "heading", self._snap(hdg))
                        self.notes.append(f"{p.cs} through the localiser: turned back onto it")
                    else:
                        self._resequence(t, p, "missed the localiser")
                        if self.cancel_missed and v.cleared:
                            self._say(p, "cancel_approach", self.rwy)
                continue
            if p.resequenced and v.cleared and v.loc:
                # Re-sequenced, but still cleared for the approach, it captured the localiser on its way round: it is
                # on the final, so it is flown as cleared (speeds included). Before 21 Nov 2026 it was still flown as
                # on its downwind, told 250 kt all the way down the glide path, and went round unstable at 246 kt.
                p.stage, p.resequenced = "cleared", False
                self.notes.append(f"{p.cs} established after all: cleared")
                continue
            if p.stage == "inbound":
                fx, fy = self.airport.xy(self.airport.holds[p.hold_fix].position)
                if not p.direct_sent:
                    self._say(p, "direct", p.hold_fix)
                    p.direct_sent = True
                if self.speeds == "rules" and p.spd_cmd is None and v.cas_kt > SPEED_HOLD + 5:
                    self._say(p, "speed", SPEED_HOLD)
                self._maybe_expedite(p, v)
                if not p.holding and distance_xy(v.x, v.y, fx, fy) < 3.0:
                    self._say(p, "hold", p.hold_fix)
                    p.holding = True
                    self.notes.append(f"{p.cs} holding at {p.hold_fix}")
                    if not p.below_stack:  # first come, first served: the back of this stack's queue
                        keys = [q.key for q in self._stack(p.hold_fix, views) if q.holding and q is not p]
                        if keys and p.key <= max(keys):
                            p.key = max(keys) + 1.0
                            self.last_plan_t = -1e9
                        self._stack_levels(views)
                if p.early and not p.holding and t >= p.retry_t:
                    level = self._free_level(p, v, views, p.early)
                    if level < STACK_BASE_FT:
                        p.below_stack = True  # out of the stack: it holds below it if it must hold after all
                    if level < v.target_alt and level != p.alt_cmd:
                        self._say(p, "altitude", level)
                    continue
                # Down into a level once the one below has left it; up to a free level only when the aircraft's own
                # level is taken nearby.
                if p.stack_level and not p.below_stack and t >= p.retry_t:
                    if p.stack_level < v.target_alt:
                        level = self._free_level(p, v, views, p.stack_level)
                        if level < v.target_alt and level != p.alt_cmd:
                            self._say(p, "altitude", level)
                    elif (p.stack_level > v.target_alt and p.alt_cmd != p.stack_level and self._level_taken(p, v, views)
                          and self._clear_above(p, v, views, p.stack_level)):
                        self._say(p, "altitude", p.stack_level)
                continue
            pts = self._points(p)
            if p.alt_cmd is None and t >= p.retry_t:
                self._say(p, "altitude", p.level)
            self._maybe_expedite(p, v)
            if p.stage == "downwind":
                if self.base_turns == "ai" and (p.ai_owns_turn or p.ai_turn_d):
                    pts = self._points(Plan(**{**vars(p), "d_join": self._turn_d(p)}))  # the AI's turn, or the RMA edge
                to_d = bearing_xy(v.x, v.y, *pts["D"])
                base_hdg = bearing_xy(*pts["D"], *pts["B"])
                self._steer(t, p, v, pts["D"])
                if (self.descent == "plan" and p.alt_cmd not in (None, self._join_level(p)) and t >= p.retry_t
                        and self._track_to_join(v, p) <= self._descent_nm(v, self._join_alt(p.d_join)) + 2.0):
                    self._say(p, "altitude", self._join_level(p))
                if p.turn_now or distance_xy(v.x, v.y, *pts["D"]) <= self._turn_lead_nm(v, to_d, base_hdg):
                    if self.base_turns == "ai" and (p.ai_owns_turn or p.ai_turn_d):
                        p.d_join, p.turn_now = self._turn_d(p), False
                    p.stage = "base"
                    self._steer(t, p, v, pts["B"], force=True)
                    self._say(p, "altitude", self._join_level(p))
                    if self.speeds == "rules":
                        self._say(p, "speed", SPEED_BASE)
            elif p.stage == "base":
                if p.alt_cmd != self._join_level(p) and t >= p.retry_t:
                    self._say(p, "altitude", self._join_level(p))
                self._steer(t, p, v, pts["B"])
                to_b = bearing_xy(v.x, v.y, *pts["B"])
                icpt = self._intercept_heading(p)
                if distance_xy(v.x, v.y, *pts["B"]) <= self._turn_lead_nm(v, to_b, icpt):
                    p.stage = "cleared"
                    self._say(p, "heading", self._snap(icpt))
                    self._say(p, "cleared_ils", self.rwy)
                    if self.speeds == "rules":
                        self._say(p, "speed", SPEED_FINAL)
        if self.speeds == "rules":
            self._final_spacing(views)
        elif self.speeds == "slot" and t - self.last_speed_t >= speed.DECISION_S:
            self.last_speed_t = t
            self._slot_speeds(views)
        return self.out

    def stage_of(self, cs: str) -> str | None:
        """The plan stage of an aircraft (inbound, downwind, base, cleared), for the speed bands."""
        p = self.plans.get(cs)
        return p.stage if p else None

    def _slot_speeds(self, views: dict) -> None:
        """The speed-only competitor: give each aircraft the speed that makes its landing slot.

        Early for its slot: the slowest speed in its band; late: the fastest; within
        SLOT_DEADBAND_S: the standard speed for its stage. Reviewed every 10 s.
        """
        for p in self.plans.values():
            v = views.get(p.cs)
            target = self.slot_target(p, v) if v else None
            if target is not None and abs(target - v.target_spd) >= 5:
                self._say(p, "speed", target)

    def slot_target(self, p: Plan, v) -> int | None:
        """The speed-only competitor's speed for one aircraft, or None where speed isn't the controller's to give."""
        limits = speed.band(p.stage, v.along)
        if limits is None:
            return None
        if p.stage == "inbound" and self.inbound_speed == "keep" and not p.holding and p.wait_s <= 0:
            return speed.in_band(limits[1], limits)  # nothing to wait for: on to the stack at the top of the band
        free_stages = {"fast": ("downwind", "cleared"), "downwind": ("downwind",),
                       "fast_safe": ("downwind", "cleared")}.get(self.free_speed, ())
        if p.stage in free_stages and p.spare_s > FREE_SPARE_S and (
                self.free_speed != "fast_safe" or p.stage != "downwind" or self.descent != "plan"
                or self._time_to_join(v, p) * min(1.0, v.cas_kt / limits[1]) >= self._descent_s(v, self._join_alt(p.d_join))):
            return self._room_to_slow(v, p, speed.in_band(limits[1], limits), limits)  # nothing ahead: top of band
        early = p.slot_t - p.landing_t  # seconds it must lose (positive) or gain (negative)
        if early > SLOT_DEADBAND_S:
            target = limits[0]
        elif early < -SLOT_DEADBAND_S:
            target = limits[1]
        else:
            target = speed.STANDARD_KT[p.stage]
        return self._room_to_slow(v, p, speed.in_band(target, limits), limits)

    def _final_room(self, v, target: int, limits: tuple[int, int]) -> int:
        """final_room: the speed brought down to the fastest on the intercept or final the slowest pilot could still
        slow from to FINAL_ROOM_TO_KT by FINAL_ROOM_AT_NM out (the distance along the course is the least it has)."""
        if not self.final_room or target <= FINAL_ROOM_TO_KT:
            return target
        room = v.along - FINAL_ROOM_AT_NM - FINAL_ROOM_MARGIN_NM
        ground = min(max(v.gs_kt / max(v.cas_kt, 100.0), 0.8), 1.5)  # miles over the ground per mile flown
        s = target
        while s > max(limits[0], FINAL_ROOM_TO_KT):
            need = (s - FINAL_ROOM_TO_KT) / FINAL_ROOM_KT_S * (s + FINAL_ROOM_TO_KT) / 2.0 * ground / 3600.0
            if need <= room:
                break
            s -= 10
        return speed.in_band(s, limits)

    def _room_to_slow(self, v, p: Plan, target: int, limits: tuple[int, int]) -> int:
        """slow_room: the speed brought down to the fastest on the downwind the aircraft could still slow from to
        SLOW_ROOM_TO_KT by its join point at SLOW_ROOM_KT_S; and final_room on the intercept and final."""
        if p.stage == "cleared":
            return self._final_room(v, target, limits)
        if not self.slow_room or p.stage != "downwind" or target <= SLOW_ROOM_TO_KT:
            return target
        room = self._track_to_join(v, p) - SLOW_ROOM_MARGIN_NM
        ground = min(max(v.gs_kt / max(v.cas_kt, 100.0), 0.8), 1.5)  # miles over the ground per mile flown
        s = target
        while s > max(limits[0], SLOW_ROOM_TO_KT):
            need = (s - SLOW_ROOM_TO_KT) / SLOW_ROOM_KT_S * (s + SLOW_ROOM_TO_KT) / 2.0 * ground / 3600.0
            if need <= room:
                break
            s -= 10
        standard = speed.STANDARD_KT["downwind"]
        if self.descent == "plan" and s > standard and v.cas_kt > 0:
            to = self._join_alt(p.d_join)
            expediting = p.want_expedite or getattr(v, "expedite", False)
            down_s = self._expedite_s(v, to, p) if expediting else self._descent_s(v, to, measured=True)
            join_s = self._time_to_join(v, p)
            while s > standard and join_s * min(1.0, v.cas_kt / s) < down_s:
                s -= 10  # faster only while it would still get down in time at that speed
        return speed.in_band(s, limits)

    def _stack(self, fix: str, views: dict) -> list[Plan]:
        """The aircraft in a stack or on their way to it (not those kept below it), in queue order."""
        return sorted((p for p in self.plans.values() if p.stage == "inbound" and p.hold_fix == fix and p.cs in views
                       and not p.below_stack), key=lambda p: p.key)

    def _stack_levels(self, views: dict) -> None:
        """The aircraft holding fill each stack from the bottom in the order they joined it; those on their way
        get the levels above."""
        for fix in STACKS:
            members = self._stack(fix, views)
            ordered = [p for p in members if p.holding] + [p for p in members if not p.holding]
            for k, p in enumerate(ordered):
                p.stack_level = STACK_BASE_FT + 1000 * k

    def _exit_conflict(self, p: Plan, v, views: dict, level: float, d_join: float) -> bool:
        """release_check="path": would releasing this aircraft now (straight towards its downwind point at its ground
        speed, descending to `level` at PLAN_DESCENT_FPM) bring it within EXIT_H_NM and EXIT_V_FT of another aircraft,
        over the next EXIT_PROBE_S, sooner than staying where it is would? Aircraft holding stay where they are, the
        others fly on along their track at their ground speed, each climbing or descending to its cleared level."""
        dx, dy = self._points(Plan(**{**vars(p), "stage": "downwind", "side": self._side(v), "d_join": d_join}))["D"]
        out_brg, out_nm = math.radians(bearing_xy(v.x, v.y, dx, dy)), distance_xy(v.x, v.y, dx, dy)
        gs = max(v.gs_kt, 150.0)

        def alt_at(a0: float, target: float, s: float) -> float:
            if target < a0:
                return max(target, a0 - PLAN_DESCENT_FPM * s / 60.0)
            return min(target, a0 + EXIT_CLIMB_FPM * s / 60.0)

        def moving(w, s: float) -> tuple[float, float]:
            d, b = w.gs_kt * s / 3600.0, math.radians(w.trk)
            return w.x + d * math.sin(b), w.y + d * math.cos(b)

        others = []
        for w in views.values():
            if w.cs == v.cs or w.loc:
                continue
            q = self.plans.get(w.cs)
            others.append((w, q is not None and q.holding))

        def first(release: bool) -> float | None:
            for k in range(1, int(EXIT_PROBE_S / EXIT_STEP_S) + 1):
                s = k * EXIT_STEP_S
                if release:
                    d = min(gs * s / 3600.0, out_nm)
                    x, y = v.x + d * math.sin(out_brg), v.y + d * math.cos(out_brg)
                    a = alt_at(v.alt_ft, min(level, v.target_alt), s)
                else:
                    x, y = (v.x, v.y) if p.holding else moving(v, s)
                    a = alt_at(v.alt_ft, v.target_alt, s)
                for w, still in others:
                    wx, wy = (w.x, w.y) if still else moving(w, s)
                    if distance_xy(x, y, wx, wy) < EXIT_H_NM and abs(alt_at(w.alt_ft, w.target_alt, s) - a) < EXIT_V_FT:
                        return s
            return None

        new = first(True)
        if new is None:
            return False
        base = first(False)
        return base is None or new < base

    def _lower_leaves_first(self, p: Plan, views: dict) -> bool:
        """An aircraft lower in this one's stack, holding, must leave first (lowest_first)."""
        if p.below_stack:
            return False
        if not p.holding:
            if self.lowest_first == "holding":
                return False
            if self.lowest_first == "zone":
                v = views[p.cs]
                fx, fy = self.airport.xy(self.airport.holds[p.hold_fix].position)
                if distance_xy(v.x, v.y, fx, fy) >= STACK_ZONE_NM:
                    return False
        return self._anyone_below_in_stack(p, views)

    def _anyone_below_in_stack(self, p: Plan, views: dict) -> bool:
        v = views[p.cs]
        return any(q.holding and q is not p and views[q.cs].alt_ft < v.alt_ft - 300 for q in self._stack(p.hold_fix, views))

    def _near(self, v, views: dict) -> list:
        return [w for w in views.values() if w.cs != v.cs and distance_xy(v.x, v.y, w.x, w.y) < VACATE_NM]

    def _level_taken(self, p: Plan, v, views: dict) -> bool:
        """Another aircraft nearby is within 1,000 ft of the level this one is heading for."""
        return any(abs(w.alt_ft - v.target_alt) < 1000 for w in self._near(v, views))

    def _clear_above(self, p: Plan, v, views: dict, level: float) -> bool:
        """No aircraft nearby is between this one and `level` (plus the separation above it)."""
        return all(w.alt_ft >= level + VACATED_FT for w in self._near(v, views) if w.alt_ft > v.alt_ft + 50)

    def _in_stack(self, p: Plan, v, views: dict) -> list:
        """stack_check="stack": the aircraft a level in this one's stack is checked against: those nearby, every
        aircraft holding at or on its way to the same stack, wherever it is, and any other aircraft of that stack
        within STACK_ZONE_NM of its fix (one that has just left it)."""
        out = {w.cs: w for w in self._near(v, views)}
        fx, fy = self.airport.xy(self.airport.holds[p.hold_fix].position)
        zone = self.stack_check == "zone"
        if zone and distance_xy(v.x, v.y, fx, fy) >= STACK_ZONE_NM:
            return list(out.values())
        for q in self.plans.values():
            if q is p or q.cs not in views or q.hold_fix != p.hold_fix:
                continue
            w = views[q.cs]
            if (not zone and q.stage == "inbound" and not q.below_stack) or distance_xy(w.x, w.y, fx, fy) < STACK_ZONE_NM:
                out[w.cs] = w
        return list(out.values())

    def _free_level(self, p: Plan, v, views: dict, want: float) -> float:
        """The lowest level from `want` up to the aircraft's current one that every aircraft below it nearby has left
        (stack_check="near"); with stack_check="stack", every aircraft below it in its stack, and that no aircraft in
        its stack is cleared to; with "holders", also above every level an aircraft holding in its stack is cleared
        to."""
        stack = self.stack_check in ("stack", "zone") and p.hold_fix
        others = self._in_stack(p, v, views) if stack else self._near(v, views)
        below = [w for w in others if w.alt_ft < v.alt_ft - 50]
        held = [w.target_alt for w in self._holders(p, v, views) if w.target_alt < v.alt_ft - 50]
        level, top = want, max(v.target_alt, want)
        while level < top:
            if all(w.alt_ft <= level - VACATED_FT for w in below) and (
                    not stack or all(abs(w.target_alt - level) >= 900 for w in others)) and (
                    all(h <= level - VACATED_FT for h in held)):
                return level
            level += 1000
        return top

    def _holders(self, p: Plan, v, views: dict) -> list:
        """stack_check="holders": the aircraft holding in this one's stack (within STACK_ZONE_NM of its fix)."""
        if self.stack_check != "holders" or not p.hold_fix:
            return []
        fx, fy = self.airport.xy(self.airport.holds[p.hold_fix].position)
        return [w for w in views.values() if w.cs != v.cs and getattr(w, "phase", "") == "holding"
                and distance_xy(w.x, w.y, fx, fy) < STACK_ZONE_NM]

    def _final_spacing(self, views: dict) -> None:
        """Final director: keep a cleared follower at least 6 NM behind the aircraft ahead.

        Speeds stay in the published band (160-180 kt until 4 DME), so the follower can
        catch up when the gap is long but can only hold 160 kt when it is short.
        """
        order = sorted((p for p in self.plans.values() if p.cs in views), key=lambda p: p.landing_t)
        for lead, follow in zip(order, order[1:]):
            lv, fv = views[lead.cs], views[follow.cs]
            if follow.stage != "cleared" or not fv.loc or fv.along < 5.0:
                continue
            want = max(TARGET_SPACING_NM, wake.separation_nm(lead.actype, follow.actype))
            # Where the follower will be when the leader lands, at the speeds both will fly from here.
            lead_s = self._eta(lv, lead)
            gap = fv.along - lead_s * self._gs(fv, 155.0, self.runway.course_true) / 3600.0
            speed = 180 if gap > want + 1.0 else SPEED_FINAL
            if follow.spd_cmd != speed:
                self._say(follow, "speed", speed)

    def _resequence(self, t: float, p: Plan, why: str) -> None:
        p.stage, p.key, p.hdg_cmd, p.alt_cmd = "downwind", t + 300.0, None, None
        p.resequenced = True
        p.rejoin_side, p.rejoin_blocked, p.min_join = 0.0, False, 0.0
        if p.straight:  # round again on the usual downwind
            p.straight, p.side = False, self.south
        self.last_plan_t = -1e9
        self.notes.append(f"{p.cs} {why}: re-sequenced")

    def on_event(self, event: dict) -> None:
        kind, cs = event.get("kind"), event.get("cs")
        if kind == "blocked":
            instr = event["instruction"]
            p = self.plans.get(instr.callsign)
            if p is not None:
                if instr.kind == "altitude":
                    p.alt_cmd = None if p.stage in ("downwind", "inbound") else 0  # ask again shortly
                    p.retry_t = event.get("t", 0) + REISSUE_S
                elif instr.kind == "heading":
                    p.hdg_cmd = None
                    if p.stage == "cleared" and p.rejoin_side:  # the turn back onto it was blocked: bring it round
                        p.rejoin_blocked = True
                    elif p.stage == "cleared":  # the turn onto the localiser was blocked: back on base, try again
                        p.stage = "base"
                elif instr.kind == "speed":
                    p.spd_cmd = None
                elif instr.kind == "expedite":
                    p.want_expedite = False  # not asked again: its path is planned on its usual descent
        elif kind == "go_around" and cs in self.plans:
            del self.plans[cs]  # re-sequenced, first in the queue, once the pilot is back at 3,000 ft
            self.went_around.add(cs)
