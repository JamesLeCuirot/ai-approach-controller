"""The safety check: a fixed, rule-based check between any controller and the aircraft.

It never learns. Before an instruction is passed on, it checks:

* altitude: not below the minimum assignable altitude (2,000 ft), and not below
  3,000 ft more than 10 NM from touchdown unless established on the localiser;
* speed: between 160 kt (until 4 DME) and 250 kt;
* separation: looking ahead two minutes, the instruction must not bring any pair
  of aircraft within 3 NM and 1,000 ft sooner than would happen without it;
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


class SafetyCheck:
    def __init__(self, airport: Airport, runway: str):
        self.airport = airport
        self.runway = airport.runways[runway]
        self.blocks = 0

    def _tracks(self, views) -> dict[str, Track]:
        return {
            v.cs: Track(v.cs, v.x, v.y, v.hdg, v.gs_kt, v.alt_ft, v.target_hdg if not v.loc else self.runway.course_true,
                        v.target_alt, v.gs, v.loc, v.tas_kt or v.gs_kt, v.wind_e_kt, v.wind_n_kt)
            for v in views
        }

    def _predict(self, tracks: dict[str, Track]) -> list[dict[str, tuple[float, float, float]]]:
        """Positions (x, y, alt) of every aircraft every STEP_S seconds over the look-ahead."""
        out = []
        state = {cs: Track(**vars(t)) for cs, t in tracks.items()}
        for _ in range(0, LOOKAHEAD_S, STEP_S):
            snap = {}
            for t in state.values():
                if t.on_localiser:  # the pilot holds the centreline whatever the wind
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

    def _rules(self, instr: Instruction, me) -> tuple[bool, str]:
        if instr.kind == "altitude":
            alt = float(instr.value)
            if alt < self.airport.min_assignable_altitude_ft:
                return False, f"below the minimum altitude of {self.airport.min_assignable_altitude_ft:,} ft"
            if alt < 3000 and me.along > 10 and not me.loc:
                return False, "below 3,000 ft more than 10 NM from touchdown"
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
        return True, ""

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
