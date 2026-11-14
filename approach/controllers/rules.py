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
INTERCEPT_DEG = 30.0
INTERCEPT_LEG_NM = 3.0
DOWNWIND_OFFSET_NM = 6.0
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
RMA_MARGIN_NM = {"D": 1.0, "B": 0.5}  # keep the planned downwind and base points this far inside the RMA
# descent="plan": height planning for pilots who descend at real rates
JOIN_ALT_FT = 3000  # the level given for the join (below the glide path there, which is 3,750 ft at 11 NM)
BELOW_GLIDE_PATH_FT = 300  # plan to reach the join point this far below the glide path, to join it from below
PLAN_DESCENT_FPM = 800.0  # assumed until the aircraft is seen descending (real arrivals: median 900, slowest tenth 630)
DESCENT_MARGIN_NM = 2.0  # time to start the descent, and a little to spare


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
    warned: bool = False
    ai_turn_d: float = 0.0  # base_turns="ai": the final length the AI chose by turning base (0: not yet)
    turn_now: bool = False  # ... and the turn is still to be given
    ai_owns_turn: bool = False  # the AI said "not yet" as the planner was about to turn it: the turn is the AI's


class RulesController(Controller):
    name = "rules"

    def __init__(self, airport: Airport, runway: str, fixed_paths: bool = False, speeds: str = "rules",
                 wind_aware: bool = False, stretch: bool = False, release: str = "careful", base_turns: str = "rule",
                 descent: str = "base", proposals: bool = False, sequence: str = "arrival"):
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
        high 31% (40 training sessions)."""
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
        self.sequence = sequence
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
        self.south = self._south_side()

    # ---- the RMA ---------------------------------------------------------------
    def _inside(self, x: float, y: float, margin: float = 0.0) -> bool:
        if self.rma is None:
            return True
        return self.rma.contains(x, y) and (margin == 0.0 or self.rma.polygon.exterior.distance(Point(x, y)) >= margin)

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
            "D": (p.d_join + lead + 1.0, p.side * DOWNWIND_OFFSET_NM),
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

    def _eta(self, v, p: Plan, d_join: float | None = None) -> float:
        """Seconds from now to touchdown on the planned path."""
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
        else:
            final = max(v.along, 0.0)
        course = self.runway.course_true
        legs += [(max(final - 4.0, 0.0), 160.0, course), (min(final, 4.0), float(wake.approach_speed(q.actype)), course)]
        return sum(d / self._gs(v, kt, trk) * 3600.0 for d, kt, trk in legs)

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

    def _height_ok(self, v, p: Plan, d_join: float) -> bool:
        """Whether the path to a join point d_join out is long enough to get down to a little below the glide path."""
        return self._track_to_join(v, p, d_join) >= self._descent_nm(v, self._join_alt(d_join))

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
        blocked = p.holding and not p.below_stack and self._anyone_below_in_stack(p, views)
        if self.descent == "plan" and not blocked:
            longest = self.max_join(side)
            blocked = longest < 0 or not self._height_ok(v, probe, max(longest, shortest))
        if self.sequence == "skip":
            return (blocked, p.key)
        return (blocked, earliest - SOONEST_FAIRNESS * max(earliest - p.key, 0.0))

    def _replan(self, t: float, views: dict) -> None:
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
        for p in order:
            v = views[p.cs]
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
                    can_go = (longest >= D_JOIN_FIXED_NM and self._inside(v.x, v.y)
                              and t + self._eta(v, probe, reach) + SPEED_ABSORB_S >= need)
                    height_reach = longest if self.stretch else D_JOIN_FIXED_NM
                else:
                    can_go = longest >= D_JOIN_MIN_NM and self._inside(v.x, v.y) and t + self._eta(v, probe, longest) >= need
                    height_reach = longest
                if can_go and self.descent == "plan" and not self._height_ok(v, probe, height_reach):
                    can_go = False  # it can't get down in time on any path inside the RMA: lower in the stack first
                if can_go and p.holding and not p.below_stack and self._anyone_below_in_stack(p, views):
                    can_go = False  # the lowest aircraft in a stack leaves first
                if can_go:
                    p.stage, p.side, p.holding = "downwind", side, False
                    ahead = sum(1 for q in self.plans.values() if q.stage == "downwind" and q.cs in views)
                    p.level = min(6000, 4000 + 1000 * ahead)
                    p.alt_cmd = None
                    p.hdg_cmd = None
                else:
                    p.landing_t = max(need, t + self._eta(v, probe, D_JOIN_MIN_NM))
                    p.slot_t = p.landing_t
                    prev_t, prev_type = p.landing_t, p.actype
                    continue
            if self.fixed_paths and p.stage != "cleared":
                if not self.stretch:
                    p.d_join = D_JOIN_FIXED_NM
                elif p.stage == "downwind":
                    p.d_join = p.ai_turn_d or self._stretched_join(t, v, p, prev_t, prev_type)
                    if self.descent == "plan" and not p.ai_turn_d:
                        p.d_join = self._join_for_height(v, p, p.d_join, max(p.d_join, self.max_join(p.side)))
                p.landing_t = t + self._eta(v, p)
                p.slot_t = p.landing_t
                if prev_t is not None:
                    gap_nm = max(TARGET_SPACING_NM, wake.separation_nm(prev_type, p.actype)) + self._margin()
                    p.slot_t = max(p.landing_t, prev_t + gap_nm / self._final_gs(v) * 3600.0)
                prev_t, prev_type = p.slot_t, p.actype
                continue
            if p.stage == "downwind" and prev_t is not None:
                gap_nm = max(TARGET_SPACING_NM, wake.separation_nm(prev_type, p.actype)) + PLANNING_MARGIN_NM
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
        d = max(p.d_join, D_JOIN_FIXED_NM)
        if prev_t is None:
            return d
        gap_nm = max(TARGET_SPACING_NM, wake.separation_nm(prev_type, p.actype)) + self._margin()
        want = prev_t + gap_nm / self._final_gs(v) * 3600.0 - SPEED_ABSORB_S  # speed takes the rest
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
                # Crossed to the far side of final without capturing the localiser: bring it round again.
                if not v.loc and v.lateral * p.side < -1.0:
                    self._resequence(t, p, "missed the localiser")
                continue
            if p.stage == "inbound":
                fx, fy = self.airport.xy(self.airport.holds[p.hold_fix].position)
                if not p.direct_sent:
                    self._say(p, "direct", p.hold_fix)
                    p.direct_sent = True
                if self.speeds == "rules" and p.spd_cmd is None and v.cas_kt > SPEED_HOLD + 5:
                    self._say(p, "speed", SPEED_HOLD)
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
            if p.stage == "downwind":
                if self.base_turns == "ai" and (p.ai_owns_turn or p.ai_turn_d):
                    pts = self._points(Plan(**{**vars(p), "d_join": self._turn_d(p)}))  # the AI's turn, or the RMA edge
                to_d = bearing_xy(v.x, v.y, *pts["D"])
                base_hdg = bearing_xy(*pts["D"], *pts["B"])
                self._steer(t, p, v, pts["D"])
                if (self.descent == "plan" and p.alt_cmd not in (None, JOIN_ALT_FT) and t >= p.retry_t
                        and self._track_to_join(v, p) <= self._descent_nm(v, self._join_alt(p.d_join)) + 2.0):
                    self._say(p, "altitude", JOIN_ALT_FT)
                if p.turn_now or distance_xy(v.x, v.y, *pts["D"]) <= self._turn_lead_nm(v, to_d, base_hdg):
                    if self.base_turns == "ai" and (p.ai_owns_turn or p.ai_turn_d):
                        p.d_join, p.turn_now = self._turn_d(p), False
                    p.stage = "base"
                    self._steer(t, p, v, pts["B"], force=True)
                    self._say(p, "altitude", 3000)
                    if self.speeds == "rules":
                        self._say(p, "speed", SPEED_BASE)
            elif p.stage == "base":
                if p.alt_cmd != 3000 and t >= p.retry_t:
                    self._say(p, "altitude", 3000)
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
        early = p.slot_t - p.landing_t  # seconds it must lose (positive) or gain (negative)
        if early > SLOT_DEADBAND_S:
            target = limits[0]
        elif early < -SLOT_DEADBAND_S:
            target = limits[1]
        else:
            target = speed.STANDARD_KT[p.stage]
        return speed.in_band(target, limits)

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

    def _free_level(self, p: Plan, v, views: dict, want: float) -> float:
        """The lowest level from `want` up to the aircraft's current one that every aircraft below it nearby has left."""
        below = [w for w in self._near(v, views) if w.alt_ft < v.alt_ft - 50]
        level, top = want, max(v.target_alt, want)
        while level < top:
            if all(w.alt_ft <= level - VACATED_FT for w in below):
                return level
            level += 1000
        return top

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
                    if p.stage == "cleared":  # the turn onto the localiser was blocked: back on base, try again
                        p.stage = "base"
                elif instr.kind == "speed":
                    p.spd_cmd = None
        elif kind == "go_around" and cs in self.plans:
            del self.plans[cs]  # re-sequenced, first in the queue, once the pilot is back at 3,000 ft
            self.went_around.add(cs)
