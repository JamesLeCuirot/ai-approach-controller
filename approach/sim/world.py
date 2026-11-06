"""The simulated world: BlueSky aircraft performance plus simulated pilots.

BlueSky runs headless and moves the aircraft. On top of it, each aircraft has a
simulated pilot that

* carries out instructions after a short delay (3-6 s),
* flies direct to a fix when told to, and flies the published hold at a fix
  (racetrack, 1-minute legs at or below FL140, 1.5 minutes above),
* captures the localiser and glide path once cleared for the ILS,
* slows to 160 kt by 7 NM and to its approach speed by 4 NM,
* goes around if the approach is unstable at 1,000 ft, the runway is still
  occupied, or it is too close to the aircraft ahead, and
* lands when it crosses the threshold on the glide path.

Wind (optional) comes from a real weather model (approach.data.gfs). Assigned
headings are flown as headings, so the wind makes aircraft drift, as it does in
real life; pilots correct for drift only when they fly a track themselves: on
the localiser and when flying direct to a fix.
"""

from __future__ import annotations

import contextlib
import io
import math
import random
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from approach import wake
from approach.airports.model import Airport
from approach.geo import FT_PER_M, KT_PER_MS, bearing_xy, heading_difference

if TYPE_CHECKING:
    from approach.data.gfs import WindProfile
from approach.instructions import Instruction

_BS_READY = False

TURN_RATE_REF_DEG_S = 2.0  # BlueSky airliner turn rate at the reference speed below
TURN_RATE_REF_TAS_KT = 247.0
RUNWAY_OCCUPANCY_S = 50.0
MIN_FINAL_GAP_NM = 2.0  # tower sends the follower around below this
MAX_CAPTURE_TRACK_DEG = 70.0  # steepest track across the localiser the autopilot still captures


def _bluesky():
    """Start BlueSky once per process (headless), or reset it for a new session."""
    global _BS_READY
    import bluesky as bs

    if not _BS_READY:
        with contextlib.redirect_stdout(io.StringIO()):
            bs.init(mode="sim", detached=True)
        _BS_READY = True
    else:
        with contextlib.redirect_stdout(io.StringIO()):
            bs.sim.reset()
    return bs


@dataclass
class AircraftView:
    """What a controller (and the safety check) can see about one aircraft."""

    cs: str
    actype: str
    wake: str
    x: float
    y: float
    alt_ft: float
    cas_kt: float
    gs_kt: float
    tas_kt: float
    hdg: float  # true
    trk: float  # true track over the ground (differs from the heading in wind)
    wind_e_kt: float  # wind at the aircraft, towards the east
    wind_n_kt: float  # wind at the aircraft, towards the north
    vs_fpm: float
    phase: str  # vectoring, established, go_around
    cleared: str | None
    along: float  # NM from the active threshold along the extended centreline
    lateral: float  # NM right (+) or left (-) of the extended centreline
    released_t: float
    target_hdg: float
    target_alt: float
    target_spd: float
    loc: bool
    gs: bool


@dataclass
class Pilot:
    cs: str
    actype: str
    released_t: float
    target_hdg: float
    target_alt: float
    target_spd: float
    phase: str = "vectoring"
    cleared: str | None = None
    loc: bool = False
    gs: bool = False
    direct_fix: str | None = None
    hold_fix: str | None = None
    hold_state: str = ""  # to_fix, turn_out, outbound, turn_in, inbound
    hold_t: float = 0.0
    pending: list[tuple[float, Instruction]] = field(default_factory=list)
    stable_checked: bool = False
    go_around_stage: int = 0
    reason: str = ""

    @property
    def wake(self) -> str:
        return wake.category(self.actype)

    @property
    def approach_speed(self) -> int:
        return wake.approach_speed(self.actype)


class World:
    def __init__(self, airport: Airport, runway: str, seed: int = 1, pilot_delay_s: tuple[float, float] = (3.0, 6.0),
                 wind: "WindProfile | None" = None):
        self.bs = _bluesky()
        from bluesky import stack

        self.stack = stack.stack
        self.airport = airport
        self.runway = airport.runways[runway]
        self.rng = random.Random(seed)
        self.pilot_delay_s = pilot_delay_s
        self.pilots: dict[str, Pilot] = {}
        self.t = 0.0
        self.last_landing_t = -1e9
        self.steps_per_second = max(1, round(1.0 / self.bs.sim.simdt))
        self.wind = wind
        if wind is not None:
            self.set_wind(wind)

    def set_wind(self, wind: "WindProfile") -> None:
        """Give BlueSky the weather model's wind on its grid, at every height level."""
        import numpy as np

        lat, lon = np.meshgrid(np.array(wind.lats), np.array(wind.lons), indexing="ij")  # south-north, west-east
        veast = np.array([np.array(g).ravel() for g in wind.u_kt]) / KT_PER_MS  # (levels, points), m/s
        vnorth = np.array([np.array(g).ravel() for g in wind.v_kt]) / KT_PER_MS
        alt_m = np.array(wind.heights_ft) / FT_PER_M
        self.bs.traf.wind.clear()
        self.bs.traf.wind.addpointvne(lat.ravel(), lon.ravel(), vnorth, veast, alt_m)
        if self.bs.traf.wind.fe is None:
            raise RuntimeError("BlueSky could not build the wind field from the weather grid")
        self.wind = wind

    # ---- traffic -----------------------------------------------------------------
    def spawn(self, cs: str, actype: str, lat: float, lon: float, hdg_true: float, alt_ft: float, cas_kt: float) -> None:
        self.stack(f"CRE {cs} {actype} {lat:.6f} {lon:.6f} {hdg_true:.1f} {alt_ft:.0f} {cas_kt:.0f}")
        self.bs.sim.step()  # process the command so the aircraft exists
        self.pilots[cs] = Pilot(cs, actype, self.t, hdg_true, alt_ft, cas_kt)

    def remove(self, cs: str) -> None:
        self.stack(f"DEL {cs}")
        self.pilots.pop(cs, None)

    def instruct(self, instr: Instruction) -> None:
        """Queue an instruction; the pilot acts on it after a short delay."""
        pilot = self.pilots.get(instr.callsign)
        if pilot is None:
            return
        delay = self.rng.uniform(*self.pilot_delay_s)
        # A pilot acts on instructions in the order they were given, so one sent with (or just after)
        # another is never carried out before it.
        due = max([self.t + delay] + [tt for tt, _ in pilot.pending])
        pilot.pending.append((due, instr))
        # Controllers and the safety check plan with the newest instruction straight away.
        if instr.kind == "heading":
            pilot.target_hdg = float(instr.value)
        elif instr.kind == "altitude":
            pilot.target_alt = float(instr.value)
        elif instr.kind == "speed":
            pilot.target_spd = float(instr.value)

    # ---- state -------------------------------------------------------------------
    def _idx(self, cs: str) -> int:
        return self.bs.traf.id2idx(cs)

    def views(self) -> list[AircraftView]:
        traf = self.bs.traf
        out = []
        for cs, p in self.pilots.items():
            i = self._idx(cs)
            if i < 0:
                continue
            x, y = self.airport.grid.to_xy(float(traf.lat[i]), float(traf.lon[i]))
            we = float(traf.windeast[i]) * KT_PER_MS if len(traf.windeast) > i else 0.0
            wn = float(traf.windnorth[i]) * KT_PER_MS if len(traf.windnorth) > i else 0.0
            along, lateral = self.airport.runway_frame(self.runway.name, x, y)
            out.append(AircraftView(
                cs=cs, actype=p.actype, wake=p.wake, x=x, y=y,
                alt_ft=float(traf.alt[i]) * FT_PER_M, cas_kt=float(traf.cas[i]) * KT_PER_MS,
                gs_kt=float(traf.gs[i]) * KT_PER_MS, tas_kt=float(traf.tas[i]) * KT_PER_MS,
                hdg=float(traf.hdg[i]) % 360.0, trk=float(traf.trk[i]) % 360.0, wind_e_kt=we, wind_n_kt=wn,
                vs_fpm=float(traf.vs[i]) * FT_PER_M * 60.0,
                phase="holding" if p.hold_fix and p.hold_state != "to_fix" else p.phase,
                cleared=p.cleared, along=along, lateral=lateral, released_t=p.released_t,
                target_hdg=p.target_hdg, target_alt=p.target_alt, target_spd=p.target_spd, loc=p.loc, gs=p.gs,
            ))
        return out

    # ---- time --------------------------------------------------------------------
    def step(self, seconds: float = 1.0) -> list[dict]:
        """Advance the world; returns events (landed, go_around, stable_check, pilot)."""
        events: list[dict] = []
        for _ in range(max(1, round(seconds))):
            for _ in range(self.steps_per_second):
                self.bs.sim.step()
            self.t += 1.0
            events += self._pilots_tick()
        return events

    def _apply(self, p: Pilot, instr: Instruction) -> None:
        cs = p.cs
        if instr.kind == "heading":
            p.direct_fix = None
            p.hold_fix = None  # a heading takes it out of the hold
            if not p.loc:
                self.stack(f"HDG {cs} {float(instr.value):.1f}")
        elif instr.kind == "altitude":
            if not p.gs:
                self.stack(f"ALT {cs} {float(instr.value):.0f}")
        elif instr.kind == "speed":
            self.stack(f"SPD {cs} {float(instr.value):.0f}")
        elif instr.kind == "direct":
            p.direct_fix = str(instr.value)
            p.hold_fix = None
        elif instr.kind == "hold" and str(instr.value) in self.airport.holds:
            p.direct_fix = None
            p.hold_fix, p.hold_state = str(instr.value), "to_fix"
        elif instr.kind == "cleared_ils":
            p.cleared = str(instr.value)

    def _pilots_tick(self) -> list[dict]:
        events: list[dict] = []
        views = {v.cs: v for v in self.views()}
        for cs, p in list(self.pilots.items()):
            v = views.get(cs)
            if v is None:
                continue
            due = [i for tt, i in p.pending if tt <= self.t]
            p.pending = [(tt, i) for tt, i in p.pending if tt > self.t]
            for instr in due:
                self._apply(p, instr)
            if p.hold_fix:
                self._hold_tick(p, v)
            elif p.direct_fix and p.direct_fix in self.airport.fixes:
                fx, fy = self.airport.xy(self.airport.fixes[p.direct_fix])
                self.stack(f"HDG {cs} {self.heading_for_track(v, bearing_xy(v.x, v.y, fx, fy)):.1f}")
            if p.phase == "go_around":
                events += self._go_around_tick(p, v)
            elif p.cleared:
                events += self._approach_tick(p, v)

        # Tower: send a follower around if it closes within 2 NM of the aircraft ahead on final.
        finals = sorted((v for v in self.views() if self.pilots[v.cs].gs), key=lambda v: v.along)
        for lead, follow in zip(finals, finals[1:]):
            if follow.along - lead.along < MIN_FINAL_GAP_NM:
                events += self._start_go_around(self.pilots[follow.cs], follow, "too close to the aircraft ahead")
        return events

    def _hold_tick(self, p: Pilot, v: AircraftView) -> None:
        """Fly the published racetrack: to the fix, turn outbound, a timed leg, turn back, inbound to the fix."""
        hold = self.airport.holds[p.hold_fix]
        fx, fy = self.airport.xy(hold.position)
        inbound = self.airport.magnetic_to_true(hold.inbound_course_mag)
        outbound = (inbound + 180.0) % 360.0
        side = 1.0 if hold.turn == "right" else -1.0
        leg_s = 60.0 if v.alt_ft <= 14000 else 90.0
        dist = math.hypot(fx - v.x, fy - v.y)
        if p.hold_state in ("to_fix", "inbound"):
            self.stack(f"HDG {p.cs} {self.heading_for_track(v, bearing_xy(v.x, v.y, fx, fy)):.1f}")
            if dist < 0.7:
                p.hold_state = "turn_out"
                self.stack(f"HDG {p.cs} {(inbound + 90.0 * side) % 360:.1f}")  # turn the published way
        elif p.hold_state == "turn_out":
            mid = (inbound + 90.0 * side) % 360
            if abs(heading_difference(v.hdg, mid)) < 15:
                self.stack(f"HDG {p.cs} {outbound:.1f}")
            if abs(heading_difference(v.hdg, outbound)) < 5:
                p.hold_state, p.hold_t = "outbound", self.t
        elif p.hold_state == "outbound":
            if self.t - p.hold_t >= leg_s:
                p.hold_state = "turn_in"
                self.stack(f"HDG {p.cs} {(outbound + 90.0 * side) % 360:.1f}")
        elif p.hold_state == "turn_in":
            mid = (outbound + 90.0 * side) % 360
            if abs(heading_difference(v.hdg, mid)) < 15:
                p.hold_state = "inbound"

    @staticmethod
    def heading_for_track(v: AircraftView, track: float) -> float:
        """The heading that makes good the given track in the wind at the aircraft."""
        tr = math.radians(track)
        cross = v.wind_e_kt * math.cos(tr) - v.wind_n_kt * math.sin(tr)  # wind component to the right of the track
        ratio = max(-0.9, min(0.9, cross / max(v.tas_kt, 100.0)))
        return (track - math.degrees(math.asin(ratio))) % 360.0

    def _turn_radius_nm(self, tas_kt: float) -> float:
        ref_radius = (TURN_RATE_REF_TAS_KT / 3600.0) / math.radians(TURN_RATE_REF_DEG_S)
        return ref_radius * (max(tas_kt, 100.0) / TURN_RATE_REF_TAS_KT) ** 2

    def _approach_tick(self, p: Pilot, v: AircraftView) -> list[dict]:
        rwy = self.runway
        c = rwy.course_true
        angle = heading_difference(v.trk, c)  # + means tracking right of the localiser course
        events: list[dict] = []
        if not p.loc:
            converging = (v.lateral < 0 < angle) or (v.lateral > 0 > angle) or abs(v.lateral) < 0.05
            lead = self._turn_radius_nm(max(v.tas_kt, v.gs_kt)) * (1 - math.cos(math.radians(abs(angle)))) + 0.05
            # Autopilots capture at steep intercepts too (the wind can make a 30 degree intercept heading a much
            # steeper track); they just overshoot more.
            if converging and abs(angle) <= MAX_CAPTURE_TRACK_DEG and abs(v.lateral) <= lead and v.along > 1.0:
                p.loc = True
                events.append({"kind": "pilot", "cs": p.cs, "text": f"{p.cs} localiser captured {v.along:.1f} NM out"})
        if p.loc:
            correction = max(-30.0, min(30.0, v.lateral * 60.0))
            self.stack(f"HDG {p.cs} {self.heading_for_track(v, (c - correction) % 360):.1f}")
            if p.phase == "vectoring" and abs(v.lateral) < 0.15 and abs(angle) < 10:
                p.phase = "established"
            gp = rwy.glide_path_altitude_ft(max(v.along, 0.0))
            if not p.gs and v.alt_ft >= gp - 30:
                p.gs = True
                self.stack(f"ALT {p.cs} {rwy.elevation_ft:.0f}")
                events.append({"kind": "pilot", "cs": p.cs, "text": f"{p.cs} glide path captured {v.along:.1f} NM out"})
            if p.gs:
                nominal = -v.gs_kt * 6076.12 / 60.0 * math.tan(math.radians(rwy.glide_slope_deg))
                vs = nominal + max(-400.0, min(400.0, (gp - v.alt_ft) * 3.0))
                self.stack(f"VS {p.cs} {vs:.0f}")
            # Pilot speed management on final
            if v.along < 7.0 and p.target_spd > 160:
                p.target_spd = 160
                self.stack(f"SPD {p.cs} 160")
            if v.along < 4.0 and p.target_spd > p.approach_speed:
                p.target_spd = p.approach_speed
                self.stack(f"SPD {p.cs} {p.approach_speed}")
            # Stability check at 1,000 ft above the aerodrome
            if not p.stable_checked and v.alt_ft <= rwy.elevation_ft + 1000:
                p.stable_checked = True
                on_path = p.gs and abs(v.alt_ft - gp) < 200
                stable = p.phase == "established" and abs(v.lateral) < 0.15 and on_path and v.cas_kt <= p.approach_speed + 25
                events.append({"kind": "stable_check", "cs": p.cs, "stable": stable,
                               "text": f"{p.cs} {'stable' if stable else 'unstable'} at 1,000 ft ({v.cas_kt:.0f} kt)"})
                if not stable:
                    return events + self._start_go_around(p, v, "unstable at 1,000 ft")
            if v.along < 0.6 and self.t - self.last_landing_t < RUNWAY_OCCUPANCY_S:
                return events + self._start_go_around(p, v, "runway still occupied")
            if p.gs and v.along <= 0.0 and v.alt_ft <= rwy.elevation_ft + 150:
                self.last_landing_t = self.t
                events.append({"kind": "landed", "cs": p.cs, "actype": p.actype,
                               "text": f"{p.cs} landed runway {rwy.name}"})
                self.remove(p.cs)
                return events
        if v.along < -0.5:
            return events + self._start_go_around(p, v, "passed the threshold without landing")
        return events

    def _start_go_around(self, p: Pilot, v: AircraftView, reason: str) -> list[dict]:
        if p.phase == "go_around" or p.cs not in self.pilots:
            return []
        p.phase, p.reason, p.cleared, p.loc, p.gs, p.go_around_stage = "go_around", reason, None, False, False, 1
        p.pending.clear()
        p.target_hdg, p.target_alt, p.target_spd = self.runway.course_true, 3000, 200
        self.stack(f"HDG {p.cs} {self.runway.course_true:.1f}")
        self.stack(f"VS {p.cs} 2000")
        self.stack(f"ALT {p.cs} 3000")
        self.stack(f"SPD {p.cs} 200")
        return [{"kind": "go_around", "cs": p.cs, "reason": reason, "text": f"{p.cs} going around: {reason}"}]

    def _go_around_tick(self, p: Pilot, v: AircraftView) -> list[dict]:
        # Published missed approach: straight ahead to 3,000 ft, then heading 178 (magnetic)
        # after passing 2,000 ft or 1 DME, whichever is later.
        if p.go_around_stage == 1 and v.alt_ft >= 2000 and v.along < 1.0:
            p.go_around_stage = 2
            p.target_hdg = self.airport.magnetic_to_true(178)
            self.stack(f"HDG {p.cs} {p.target_hdg:.1f}")
        if p.go_around_stage == 2 and v.alt_ft >= 2800:
            p.phase, p.go_around_stage = "vectoring", 0
            p.stable_checked = False
            return [{"kind": "pilot", "cs": p.cs, "text": f"{p.cs} back with the final director at 3,000 ft"}]
        return []
