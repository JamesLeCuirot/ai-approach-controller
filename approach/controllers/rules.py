"""The rules controller: the hand-written baseline every learned controller must beat.

It follows the stage 0 spec (docs/stage0-gatwick-spec.md):

1. Sequence: landing order by each aircraft's earliest possible landing time when
   it is released; each slot is the slot ahead plus the larger of 6 NM and the
   wake minimum (as time at final approach speed).
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

Positions are worked in the runway frame: ``along`` (NM out from the threshold on
the extended centreline) and ``lateral`` (NM right of the centreline as seen by an
aircraft on final; negative is left).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from approach import wake
from approach.airports.model import Airport
from approach.controllers.base import Controller
from approach.geo import bearing_xy, distance_xy, heading_difference
from approach.instructions import Instruction

D_JOIN_MIN_NM = 11.0
D_JOIN_MAX_NM = 30.0
INTERCEPT_DEG = 30.0
INTERCEPT_LEG_NM = 3.0
DOWNWIND_OFFSET_NM = 6.0
TARGET_SPACING_NM = 6.0  # one departure between arrivals
PLANNING_MARGIN_NM = 0.5  # plan slightly long: the final director can close a gap but not open one
SPEED_DOWNWIND, SPEED_BASE, SPEED_FINAL = 220, 180, 160
GS_DOWNWIND, GS_BASE = 235.0, 190.0  # rough ground speeds used for timing (no wind)
REISSUE_DEG = 8.0
REISSUE_S = 20.0
PILOT_DELAY_S = 4.5


@dataclass
class Plan:
    cs: str
    actype: str
    key: float  # earliest landing time when first planned: fixes the order
    side: float  # -1 = left of the centreline (south for 26L), +1 = right
    stage: str = "downwind"  # downwind -> base -> cleared
    d_join: float = D_JOIN_MIN_NM
    level: int = 4000
    landing_t: float = 0.0
    hdg_cmd: float | None = None
    hdg_t: float = -1e9
    alt_cmd: float | None = None
    spd_cmd: float | None = None
    retry_t: float = -1e9  # after a blocked altitude, wait before asking again
    warned: bool = False


class RulesController(Controller):
    name = "rules"

    def __init__(self, airport: Airport, runway: str):
        self.airport = airport
        self.runway = airport.runways[runway]
        self.rwy = runway
        self.plans: dict[str, Plan] = {}
        self.last_plan_t = -1e9
        self.out: list[Instruction] = []
        self.notes: list[str] = []

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
        if q.stage == "downwind":
            downwind_gs = max(v.tas_kt, 200.0)  # true airspeed is faster than indicated at altitude
            to_d = bearing_xy(v.x, v.y, *pts["D"])
            legs += [
                (distance_xy(v.x, v.y, *pts["D"]) - self._corner_saving(to_d, base_hdg, downwind_gs), downwind_gs),
                (distance_xy(*pts["D"], *pts["B"]) - self._corner_saving(base_hdg, icpt, GS_BASE), GS_BASE),
            ]
        elif q.stage == "base":
            to_b = bearing_xy(v.x, v.y, *pts["B"])
            legs.append((distance_xy(v.x, v.y, *pts["B"]) - self._corner_saving(to_b, icpt, GS_BASE), GS_BASE))
        if q.stage in ("downwind", "base"):
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
        order = sorted((p for p in self.plans.values() if p.cs in views), key=lambda p: (p.stage != "cleared", p.key))
        prev_t, prev_type = None, None
        for p in order:
            v = views[p.cs]
            if p.stage == "downwind" and prev_t is not None:
                gap_nm = max(TARGET_SPACING_NM, wake.separation_nm(prev_type, p.actype)) + PLANNING_MARGIN_NM
                need = prev_t + gap_nm / 150.0 * 3600.0  # landing time that keeps the gap
                lo, hi = D_JOIN_MIN_NM, D_JOIN_MAX_NM
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
                side = -1.0 if v.lateral < 0 else 1.0
                p = Plan(v.cs, v.actype, key=0.0, side=side)
                p.key = t + self._eta(v, p)
                ahead = sum(1 for q in self.plans.values() if q.stage == "downwind" and q.cs in views)
                p.level = min(6000, 4000 + 1000 * ahead)
                self.plans[v.cs] = p
                self.last_plan_t = -1e9  # replan now
        for cs in [cs for cs in self.plans if cs not in views]:
            del self.plans[cs]  # landed
        if t - self.last_plan_t >= 30:
            self._replan(t, views)
            self.last_plan_t = t

        for p in sorted(self.plans.values(), key=lambda p: p.key):
            v = views[p.cs]
            if p.stage == "cleared":
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
                    self._say(p, "speed", SPEED_FINAL)
        self._final_spacing(views)
        return self.out

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

    def on_event(self, event: dict) -> None:
        kind, cs = event.get("kind"), event.get("cs")
        if kind == "blocked":
            instr = event["instruction"]
            p = self.plans.get(instr.callsign)
            if p is not None:
                if instr.kind == "altitude":
                    p.alt_cmd = None if p.stage == "downwind" else 0  # ask again shortly
                    p.retry_t = event.get("t", 0) + REISSUE_S
                elif instr.kind == "heading":
                    p.hdg_cmd = None
                elif instr.kind == "speed":
                    p.spd_cmd = None
        elif kind == "go_around" and cs in self.plans:
            del self.plans[cs]  # re-sequenced once the pilot is back at 3,000 ft
