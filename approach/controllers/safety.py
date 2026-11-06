"""The safety check: a fixed, rule-based check between any controller and the aircraft.

It never learns. Before an instruction is passed on, it checks:

* altitude: not below the minimum altitude where the aircraft is and where it is
  heading over the next two minutes (2,000 ft inside the minimum-altitude area,
  3,000 ft outside it), unless it is established on the localiser;
* speed: between 160 kt (until 4 DME) and 250 kt;
* separation: looking ahead two minutes, the instruction must not bring any pair
  of aircraft within 3 NM and 1,000 ft sooner than would happen without it;
* the RMA (radar manoeuvring area) for the runway in use: once an aircraft is
  inside it, a heading must keep it inside for the next minute, or until it crosses
  the final approach track, and must not leave it worse off than its current
  heading. Outside it, an aircraft may only be turned towards it; otherwise it
  stays on its route, goes direct to a fix or holds;
* airspace: a heading must not take the aircraft more than 40 NM from the airport.

Every block is logged with its reason, and counted in the scores.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from approach.airports.model import Airport
from approach.geo import heading_difference
from approach.instructions import Instruction

LOOKAHEAD_S = 120
STEP_S = 5
H_SEP_NM = 3.0
V_SEP_FT = 1000.0
TURN_RATE_DEG_S = 2.0
CLIMB_FT_S = 25.0
AREA_RADIUS_NM = 40.0
RMA_TOLERANCE_NM = 0.5  # how far a predicted path may stray over the RMA's edge
RMA_LOOKAHEAD_S = 60  # a heading must keep the aircraft inside for the next minute: the controller turns it again


@dataclass
class Track:
    cs: str
    x: float
    y: float
    hdg: float
    gs_kt: float
    alt: float
    target_hdg: float
    target_alt: float
    on_glide_path: bool
    on_localiser: bool = False
    tas_kt: float = 0.0
    wind_e_kt: float = 0.0  # wind at the aircraft (towards east, towards north)
    wind_n_kt: float = 0.0
    holding: bool = False  # flying a hold: stays near its fix


class SafetyCheck:
    def __init__(self, airport: Airport, runway: str):
        self.airport = airport
        self.runway = airport.runways[runway]
        self.blocks = 0

    def _tracks(self, views) -> dict[str, Track]:
        return {
            v.cs: Track(v.cs, v.x, v.y, v.hdg, v.gs_kt, v.alt_ft, v.target_hdg if not v.loc else self.runway.course_true,
                        v.target_alt, v.gs, v.loc, v.tas_kt or v.gs_kt, v.wind_e_kt, v.wind_n_kt,
                        getattr(v, "phase", "") == "holding")
            for v in views
        }

    def _predict(self, tracks: dict[str, Track]) -> list[dict[str, tuple[float, float, float]]]:
        """Positions (x, y, alt) of every aircraft every STEP_S seconds over the look-ahead."""
        out = []
        state = {cs: Track(**vars(t)) for cs, t in tracks.items()}
        for _ in range(0, LOOKAHEAD_S, STEP_S):
            snap = {}
            for t in state.values():
                if t.holding:  # circling a fix: stays close to where it is
                    pass
                elif t.on_localiser:  # the pilot holds the centreline whatever the wind
                    d = t.gs_kt * STEP_S / 3600.0
                    t.x += d * math.sin(math.radians(self.runway.course_true))
                    t.y += d * math.cos(math.radians(self.runway.course_true))
                else:  # flies the heading through the air and drifts with the wind
                    turn = heading_difference(t.target_hdg, t.hdg)
                    t.hdg = (t.hdg + max(-TURN_RATE_DEG_S * STEP_S, min(TURN_RATE_DEG_S * STEP_S, turn))) % 360
                    k = STEP_S / 3600.0
                    t.x += (t.tas_kt * math.sin(math.radians(t.hdg)) + t.wind_e_kt) * k
                    t.y += (t.tas_kt * math.cos(math.radians(t.hdg)) + t.wind_n_kt) * k
                if t.on_glide_path:
                    along, _ = self.airport.runway_frame(self.runway.name, t.x, t.y)
                    t.alt = self.runway.glide_path_altitude_ft(max(along, 0.0))
                else:
                    dz = t.target_alt - t.alt
                    t.alt += max(-CLIMB_FT_S * STEP_S, min(CLIMB_FT_S * STEP_S, dz))
                snap[t.cs] = (t.x, t.y, t.alt)
            out.append(snap)
        return out

    @staticmethod
    def _first_conflict(pred, involving: str) -> tuple[int, str, float, float] | None:
        for k, snap in enumerate(pred):
            if involving not in snap:
                continue
            x, y, a = snap[involving]
            for other, (ox, oy, oa) in snap.items():
                if other == involving:
                    continue
                h, v = math.hypot(ox - x, oy - y), abs(oa - a)
                if h < H_SEP_NM and v < V_SEP_FT:
                    return k, other, h, v
        return None

    def review(self, instr: Instruction, views) -> tuple[bool, str]:
        by_cs = {v.cs: v for v in views}
        me = by_cs.get(instr.callsign)
        if me is None:
            return False, "no such aircraft"
        ok, reason = self._rules(instr, me)
        if ok and instr.kind in ("heading", "altitude"):
            ok, reason = self._separation(instr, views)
        if not ok:
            self.blocks += 1
        return ok, reason

    def _path(self, me, target_hdg: float, seconds: int = LOOKAHEAD_S) -> list[tuple[float, float]]:
        """Where one aircraft goes over the look-ahead on a heading, turning at the standard rate, in the wind."""
        x, y, hdg = me.x, me.y, me.hdg
        tas = me.tas_kt or me.gs_kt
        out = []
        for _ in range(0, seconds, STEP_S):
            turn = heading_difference(target_hdg, hdg)
            hdg = (hdg + max(-TURN_RATE_DEG_S * STEP_S, min(TURN_RATE_DEG_S * STEP_S, turn))) % 360
            k = STEP_S / 3600.0
            x += (tas * math.sin(math.radians(hdg)) + me.wind_e_kt) * k
            y += (tas * math.cos(math.radians(hdg)) + me.wind_n_kt) * k
            out.append((x, y))
        return out

    def _rules(self, instr: Instruction, me) -> tuple[bool, str]:
        if instr.kind == "altitude":
            alt = float(instr.value)
            if alt < self.airport.min_assignable_altitude_ft:
                return False, f"below the minimum altitude of {self.airport.min_assignable_altitude_ft:,} ft"
            if not me.loc:
                ahead = [(me.x, me.y)] + self._path(me, me.target_hdg)[5::6]
                need = max(self.airport.minimum_altitude_ft(x, y) for x, y in ahead)
                if alt < need:
                    return False, f"below the minimum altitude of {need:,} ft for where it is going"
        if instr.kind == "speed":
            spd = float(instr.value)
            if spd > 250:
                return False, "above 250 kt"
            if spd < 160 and me.along > 4:
                return False, "below 160 kt before 4 DME"
        if instr.kind == "heading":
            h = math.radians(float(instr.value))
            k = LOOKAHEAD_S / 3600.0
            tas = me.tas_kt or me.gs_kt
            x = me.x + (tas * math.sin(h) + me.wind_e_kt) * k
            y = me.y + (tas * math.cos(h) + me.wind_n_kt) * k
            if math.hypot(x, y) > AREA_RADIUS_NM:
                return False, "would take the aircraft out of Gatwick's airspace"
            rma = self.airport.rma(self.runway.name)
            if rma is not None and not me.loc:
                new, cur = self._rma_path(me, float(instr.value)), self._rma_path(me, me.target_hdg)
                if rma.contains(me.x, me.y):
                    worst = max(rma.distance_nm(px, py) for px, py in new)
                    worst_now = max(rma.distance_nm(px, py) for px, py in cur)
                    if worst > RMA_TOLERANCE_NM and worst >= worst_now - 0.1:
                        return False, f"would take it {worst:.1f} NM outside the RMA"
                else:
                    before = rma.distance_nm(me.x, me.y)
                    after, after_now = rma.distance_nm(*new[-1]), rma.distance_nm(*cur[-1])
                    flown = (me.tas_kt or me.gs_kt) * RMA_LOOKAHEAD_S / 3600.0
                    towards = after == 0 or before - after >= 0.3 * flown or after + 0.5 < after_now
                    if not towards:
                        return False, "outside the RMA: it may only be turned towards it, or sent direct to a fix or held"
        return True, ""

    def _rma_path(self, me, hdg: float) -> list[tuple[float, float]]:
        """The next minute on a heading, cut short where it crosses the final approach track (it joins final there,
        or the controller turns it)."""
        path = self._path(me, hdg, RMA_LOOKAHEAD_S)
        _, lat0 = self.airport.runway_frame(self.runway.name, me.x, me.y)
        for k, (px, py) in enumerate(path):
            along, lat = self.airport.runway_frame(self.runway.name, px, py)
            if 0 < along < 25 and (lat == 0 or (lat > 0) != (lat0 > 0)):
                return path[: k + 1]
        return path

    def _separation(self, instr: Instruction, views) -> tuple[bool, str]:
        tracks = self._tracks(views)
        base = self._first_conflict(self._predict(tracks), instr.callsign)
        t = tracks[instr.callsign]
        if instr.kind == "heading":
            t.target_hdg = float(instr.value)
        else:
            t.target_alt = float(instr.value)
        new = self._first_conflict(self._predict(tracks), instr.callsign)
        if new is None or (base is not None and base[0] <= new[0]):
            return True, ""
        k, other, h, v = new
        return False, f"would bring it within {h:.1f} NM and {v:,.0f} ft of {other} in about {(k + 1) * STEP_S} s"
