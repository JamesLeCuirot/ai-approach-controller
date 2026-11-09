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
10 s it steps each aircraft's speed towards its landing slot, with the same steps and
bands as the AI, from approach.controllers.speed) or "none" (the AI sets them).

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
SPEED_DOWNWIND, SPEED_BASE, SPEED_FINAL = 220, 180, 160
GS_DOWNWIND, GS_BASE = 235.0, 190.0  # rough ground speeds used for timing (no wind)
REISSUE_DEG = 8.0
REISSUE_S = 20.0
PILOT_DELAY_S = 4.5
STACKS = ("WILLO", "TIMBA")
STACK_BASE_FT = 7000  # the minimum stack level (FL70 on a standard day)
SPEED_HOLD = 220
RMA_MARGIN_NM = {"D": 1.0, "B": 0.5}  # keep the planned downwind and base points this far inside the RMA


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


class RulesController(Controller):
    name = "rules"

    def __init__(self, airport: Airport, runway: str, fixed_paths: bool = False, speeds: str = "rules"):
        if speeds not in ("rules", "slot", "none"):
            raise ValueError(f"speeds must be rules, slot or none, not {speeds!r}")
        self.fixed_paths = fixed_paths
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

    def _eta(self, v, p: Plan, d_join: float | None = None) -> float:
        """Seconds from now to touchdown on the planned path."""
        q = Plan(**{**vars(p), "d_join": d_join if d_join is not None else p.d_join})
        pts = self._points(q)
        legs = []
        icpt = self._intercept_heading(q)
        base_hdg = bearing_xy(*pts["D"], *pts["B"])
        if q.stage in ("downwind", "inbound"):
            downwind_gs = max(v.tas_kt, 200.0)  # true airspeed is faster than indicated at altitude
            to_d = bearing_xy(v.x, v.y, *pts["D"])
            legs += [
                (distance_xy(v.x, v.y, *pts["D"]) - self._corner_saving(to_d, base_hdg, downwind_gs), downwind_gs),
                (distance_xy(*pts["D"], *pts["B"]) - self._corner_saving(base_hdg, icpt, GS_BASE), GS_BASE),
            ]
        elif q.stage == "base":
            to_b = bearing_xy(v.x, v.y, *pts["B"])
            legs.append((distance_xy(v.x, v.y, *pts["B"]) - self._corner_saving(to_b, icpt, GS_BASE), GS_BASE))
        if q.stage in ("downwind", "base", "inbound"):
            legs.append((INTERCEPT_LEG_NM, GS_BASE))
            final = q.d_join
        else:
            final = max(v.along, 0.0)
        legs += [(max(final - 4.0, 0.0), 160.0), (min(final, 4.0), float(wake.approach_speed(q.actype)))]
        return sum(d / gs * 3600.0 for d, gs in legs)

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

    def _snap(self, true_hdg: float) -> float:
        """Round to a 5 degree magnetic heading, as a controller would say it."""
        mag = round(self.airport.true_to_magnetic(true_hdg) / 5.0) * 5.0
        return self.airport.magnetic_to_true(mag % 360)

    # ---- sequencing ------------------------------------------------------------
    def _replan(self, t: float, views: dict) -> None:
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
                    gap_nm = max(TARGET_SPACING_NM, wake.separation_nm(prev_type, p.actype)) + PLANNING_MARGIN_NM
                    need = prev_t + gap_nm / 150.0 * 3600.0
                if self.fixed_paths:
                    probe.d_join = D_JOIN_FIXED_NM
                    can_go = (longest >= D_JOIN_FIXED_NM and self._inside(v.x, v.y)
                              and t + self._eta(v, probe) + SPEED_ABSORB_S >= need)
                else:
                    can_go = longest >= D_JOIN_MIN_NM and self._inside(v.x, v.y) and t + self._eta(v, probe, longest) >= need
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
                p.d_join = D_JOIN_FIXED_NM
                p.landing_t = t + self._eta(v, p)
                p.slot_t = p.landing_t
                if prev_t is not None:
                    gap_nm = max(TARGET_SPACING_NM, wake.separation_nm(prev_type, p.actype)) + PLANNING_MARGIN_NM
                    p.slot_t = max(p.landing_t, prev_t + gap_nm / 150.0 * 3600.0)
                prev_t, prev_type = p.slot_t, p.actype
                continue
            if p.stage == "downwind" and prev_t is not None:
                gap_nm = max(TARGET_SPACING_NM, wake.separation_nm(prev_type, p.actype)) + PLANNING_MARGIN_NM
                need = prev_t + gap_nm / 150.0 * 3600.0  # landing time that keeps the gap
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
            p.landing_t = t + self._eta(v, p)
            p.slot_t = p.landing_t
            prev_t, prev_type = p.landing_t, p.actype

    # ---- instructions ----------------------------------------------------------
    def _say(self, p: Plan, kind: str, value) -> None:
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
                # Stacks only step down, except for an aircraft coming up from below them (after a go-around),
                # which climbs to its level in the stack.
                climb_into_stack = v.target_alt < STACK_BASE_FT
                if (p.stack_level and p.alt_cmd != p.stack_level and t >= p.retry_t
                        and (p.stack_level < v.target_alt or climb_into_stack)):
                    self._say(p, "altitude", p.stack_level)
                continue
            pts = self._points(p)
            if p.alt_cmd is None and t >= p.retry_t:
                self._say(p, "altitude", p.level)
            if p.stage == "downwind":
                to_d = bearing_xy(v.x, v.y, *pts["D"])
                base_hdg = bearing_xy(*pts["D"], *pts["B"])
                self._steer(t, p, v, pts["D"])
                if distance_xy(v.x, v.y, *pts["D"]) <= self._turn_lead_nm(v, to_d, base_hdg):
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
        """The speed-only competitor: step each aircraft's speed towards making its landing slot.

        Early for its slot: slow down; late: speed up; within SLOT_DEADBAND_S: back towards
        the standard speed for its stage. One step (10 or 20 kt) per aircraft every 10 s.
        """
        for p in self.plans.values():
            v = views.get(p.cs)
            limits = speed.band(p.stage, v.along) if v else None
            if limits is None:
                continue
            new = speed.apply_step(v.target_spd, self.slot_step(p, v), limits)
            if abs(new - v.target_spd) >= 5:
                self._say(p, "speed", new)

    def slot_step(self, p: Plan, v) -> int:
        """The speed-only competitor's step for one aircraft: towards the speed that makes its slot."""
        limits = speed.band(p.stage, v.along)
        if limits is None:
            return 0
        early = p.slot_t - p.landing_t  # seconds it must lose (positive) or gain (negative)
        if early > SLOT_DEADBAND_S:
            target = limits[0]
        elif early < -SLOT_DEADBAND_S:
            target = limits[1]
        else:
            target = speed.STANDARD_KT[p.stage]
        diff = target - v.target_spd
        return 0 if abs(diff) < 5 else int(max(-20, min(20, round(diff / 10.0) * 10)))

    def _stack_levels(self, views: dict) -> None:
        """Each stack fills from the bottom: the earliest in the sequence gets the lowest level."""
        for fix in STACKS:
            waiting = sorted((p for p in self.plans.values() if p.stage == "inbound" and p.hold_fix == fix and p.cs in views),
                             key=lambda p: p.key)
            for k, p in enumerate(waiting):
                p.stack_level = STACK_BASE_FT + 1000 * k

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
            gap = fv.along - lead_s * 155.0 / 3600.0
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
