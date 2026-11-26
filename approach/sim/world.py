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

How hard aircraft turn, how fast they descend and how fast they change speed
depend on ``pilots``:

* ``"bluesky"`` (the default, which every result before stage 2 used):
  BlueSky's own values, the same for every aircraft: 25 degrees of bank,
  1,500 ft/min, and speed changes of 2 to 3 kt a second.
* ``"measured"``: each pilot gets its own bank angle, descent rate and rates of
  slowing down and speeding up (including from 160 kt to its approach speed on
  final, which it starts at 5 NM, as real pilots do), drawn from what real
  Gatwick arrivals do
  (approach.data.response, data/real/<airport>/response.json). Real arrivals
  bank about 23 degrees in large turns and less in small ones (about 10 degrees
  for a 10-15 degree turn: autopilots bank in proportion to the heading still to
  turn, up to a limit), descend at about 900 ft/min and slow down at about
  0.6 kt a second in level flight and 0.45 descending.
"""

from __future__ import annotations

import contextlib
import functools
import io
import json
import math
import os
import random
from dataclasses import dataclass, field
from pathlib import Path
from statistics import NormalDist
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
# The longest a turn in a hold takes before the pilot carries on regardless: half a turn at the lowest measured bank
# (15 degrees) and holding speed high up takes about 3 minutes; the turn back towards the inbound leg is a quarter turn.
HOLD_TURN_MAX_S = {"turn_out": 240.0, "turn_in": 150.0}
WIND_CEILING_FT = 45000.0  # the weather model's top-level wind is given to BlueSky up to here
MIN_FINAL_GAP_NM = 2.0  # tower sends the follower around below this
MAX_CAPTURE_TRACK_DEG = 70.0  # steepest track across the localiser the autopilot still captures


# BlueSky's time step. Its own default is 0.05 s; 0.5 s gives the same results on the stage 0 tests and the Storm
# Darragh hour, about ten times faster, and it is used for training and the benchmark alike (same physics in both).
SIM_DT_S = float(os.environ.get("APPROACH_SIM_DT", "0.5"))

PILOT_MODELS = ("bluesky", "measured", "drifting", "drifting_handover")
# "drifting": measured pilots whose place in the spread of descent rates wanders as real crews' does. Measured from the
# height real arrivals lose over 60 s windows within their descents, on 12 training days (5,965 descents of 2 minutes
# or more; approach.data.response, 25 Nov 2026): the correlation of a crew's place (normal scores) between windows 60,
# 120, 240 and 360 s apart is 0.52, 0.24, 0.10 and 0.12, against 1 for a "measured" pilot, which keeps its place for
# the whole flight. A place that drifts back to the middle with a time constant of 50 s, plus a lasting part (7% of the
# spread) for the crew's own habit, fits all six within 0.02. So of the slowest fifth, 39% are still in the slowest
# fifth 2 minutes later, not all of them.
# "drifting_handover": drifting pilots that start where their real crews were at handover. 82% of real arrivals handed
# over at FL120 or above are already descending (a median 1,408 ft/min), and how fast tells a controller a lot: on the
# training days, 9% of those handed over level on the approach side more than 8 NM off the final approach line were
# brought straight in, against 26% at 1,000 to 1,500 ft/min and 53% faster than 2,000 (approach.data.arrivals'
# handover state, 25 Nov 2026). A simulated pilot used to start level, at a place in the spread drawn independently of
# its real crew's. This one starts at the place in the usual spread at its height that its real crew's rate puts it,
# then drifts as the others do. It still starts level, and descends once the controller clears it lower: the level the
# real crew was cleared to before handover isn't known, and starting it down to the stacks' lowest level (FL70) brought
# arrivals down into a stack's holders before the planner had given them its own level (session 33 of the 48 training
# sessions: 5 losses of separation, and the stack locked up).
DRIFT_TAU_S = 50.0
DRIFT_LASTING = 0.07
DRIFT_EVERY_S = 5.0  # how often a drifting pilot's place moves
_NORMAL = NormalDist()
FPM_TO_MS = 0.00508
REAL_DIR = Path(__file__).resolve().parents[2] / "data" / "real"
# Physical bounds on each measured quantity, for the tails beyond the 10th and 90th percentiles.
_BOUNDS = {"bank_deg": (15.0, 26.0), "descent_fpm": (400.0, 1800.0), "decel_kt_s": (0.2, 1.2),
           "decel_desc_kt_s": (0.15, 1.0), "accel_kt_s": (0.15, 1.0)}
FINAL_SLOWING_BOUNDS = (0.05, 1.2)  # kt/s
# Measured pilots descend at a rate that depends on height, as real arrivals do (approach.data.response,
# descent.by_height_ft: a median 783 ft/min between 3,500 and 7,000 ft, 1,010 between 7,000 and 11,000 and 1,287
# above, the fastest tenth near 2,000 high up). Each pilot keeps its place in the spread at every height. Before
# 16 November 2026 a pilot descended at one rate, drawn from the 3,500 to 11,000 ft measurements, at every height.
DESCENT_BY_HEIGHT_BOUNDS = (400.0, 2500.0)
HANDOVER_DESCENT_FPM = 300.0  # "drifting_handover": a real rate at handover this fast or faster is a descent
EXPEDITE_DONE_FT = 200.0  # an expedited descent ends this close above the cleared level
APPROACH_SPEED_FROM_NM = {"bluesky": 4.0, "measured": 5.0, "drifting": 5.0, "drifting_handover": 5.0}  # real pilots slow to their approach speed from about 5 NM
# Degrees of bank per degree of heading still to turn, up to the pilot's limit. Measured on a 5 s grid as the real
# turns are, simulated turns of 20, 35, 60 and 120 degrees then bank 14, 19, 22.5 and 23 degrees; real ones of
# 15-25, 25-45, 45-90 and over 90 degrees bank 13.6, 19.3, 22.4 and 23.7.
BANK_PER_DEG = 1.2
MIN_BANK_DEG = 3.0


@functools.lru_cache(maxsize=None)
def response_quantiles(airport_icao: str) -> dict[str, dict[str, float]]:
    """The measured quantiles each pilot is drawn from (approach.data.response)."""
    r = json.loads((REAL_DIR / airport_icao.lower() / "response.json").read_text())
    return {"bank_deg": r["turns"]["bank_deg"], "descent_fpm": r["descent"]["rate_fpm"],
            "decel_kt_s": r["speed"]["decel_level_kt_s"], "decel_desc_kt_s": r["speed"]["decel_descending_kt_s"],
            "accel_kt_s": r["speed"]["accel_kt_s"]}


@functools.lru_cache(maxsize=None)
def descent_by_height(airport_icao: str) -> tuple[tuple[float, dict[str, float]], ...]:
    """The measured descent rates by height band: (the band's middle in ft, its quantiles), lowest first; empty if
    the airport's measurements have none."""
    r = json.loads((REAL_DIR / airport_icao.lower() / "response.json").read_text())
    bands = []
    for key, q in r["descent"].get("by_height_ft", {}).items():
        lo, hi = (float(x) for x in key.split("-"))
        bands.append(((lo + hi) / 2.0, q))
    return tuple(sorted(bands, key=lambda b: b[0]))


@functools.lru_cache(maxsize=None)
def expedite_by_height(airport_icao: str) -> tuple[tuple[float, dict[str, float]], ...]:
    """How fast real arrivals descend in each height band while short of distance (approach.data.response,
    short_by_height_ft), lowest first, in the same bands as descent_by_height; empty if not measured."""
    r = json.loads((REAL_DIR / airport_icao.lower() / "response.json").read_text())
    bands = []
    for key, q in r["descent"].get("short_by_height_ft", {}).items():
        lo, hi = (float(x) for x in key.split("-"))
        bands.append(((lo + hi) / 2.0, q))
    return tuple(sorted(bands, key=lambda b: b[0]))


def expedite_profile(airport_icao: str, u: float) -> tuple[float, ...]:
    """A pilot's rate in each height band once told to expedite its descent: at the same cumulative probability u
    as its usual rate, from the rates real arrivals reach when short of distance, and never slower than its usual
    rate (low down, where they are slowing down too, real arrivals short of distance descend no faster)."""
    usual = descent_profile(airport_icao, u)
    short = expedite_by_height(airport_icao)
    if len(short) != len(usual):
        return usual
    return tuple(max(a, draw(q, u, DESCENT_BY_HEIGHT_BOUNDS)) for a, (_, q) in zip(usual, short))


def descent_profile(airport_icao: str, u: float) -> tuple[float, ...]:
    """A pilot's descent rate (ft/min) at the middle of each height band, at cumulative probability u in every band:
    a pilot who descends fast high up descends fast lower down too."""
    return tuple(draw(q, u, DESCENT_BY_HEIGHT_BOUNDS) for _, q in descent_by_height(airport_icao))


@functools.lru_cache(maxsize=4096)
def profiles_at(airport_icao: str, u: float) -> tuple[tuple[float, ...], tuple[float, ...]]:
    """A pilot's usual and expedited descent profiles at cumulative probability u (cached: drifting pilots move)."""
    return descent_profile(airport_icao, u), expedite_profile(airport_icao, u)


def drift_start(u: float, rng: random.Random) -> tuple[float, float]:
    """A drifting pilot's two parts at a first place u in the spread: the lasting part (the crew's habit) and the part
    that drifts, as normal scores, drawn so that together they put it at u."""
    z = _NORMAL.inv_cdf(min(max(u, 1e-6), 1 - 1e-6))
    lasting = math.sqrt(DRIFT_LASTING) * z + math.sqrt(1.0 - DRIFT_LASTING) * rng.gauss(0.0, 1.0)
    drifting = (z - math.sqrt(DRIFT_LASTING) * lasting) / math.sqrt(1.0 - DRIFT_LASTING)
    return lasting, drifting


def place_of(airport_icao: str, rate_fpm: float, alt_ft: float) -> float:
    """The place u in the usual spread of descent rates at a height (descent_profile) of a pilot descending at
    rate_fpm there: the inverse of descent_rate_at(descent_profile(u)), clipped to [0.005, 0.995]."""
    lo, hi = 0.005, 0.995
    for _ in range(30):
        mid = (lo + hi) / 2.0
        if descent_rate_at(airport_icao, descent_profile(airport_icao, mid), alt_ft) < rate_fpm:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2.0


def drift_step(lasting: float, drifting: float, dt: float, rng: random.Random) -> tuple[float, float]:
    """The drifting part dt seconds on (back towards the middle with time constant DRIFT_TAU_S, the spread kept), and
    the pilot's place in the spread then."""
    a = math.exp(-dt / DRIFT_TAU_S)
    drifting = a * drifting + math.sqrt(1.0 - a * a) * rng.gauss(0.0, 1.0)
    return drifting, _NORMAL.cdf(math.sqrt(DRIFT_LASTING) * lasting + math.sqrt(1.0 - DRIFT_LASTING) * drifting)


def descent_rate_at(airport_icao: str, profile: tuple[float, ...], alt_ft: float) -> float:
    """The rate at a height, linear between the bands' middles and level beyond them."""
    mids = [m for m, _ in descent_by_height(airport_icao)]
    if alt_ft <= mids[0]:
        return profile[0]
    for k in range(len(mids) - 1):
        if alt_ft <= mids[k + 1]:
            f = (alt_ft - mids[k]) / (mids[k + 1] - mids[k])
            return profile[k] + f * (profile[k + 1] - profile[k])
    return profile[-1]


@functools.lru_cache(maxsize=None)
def final_slowing_quantiles(airport_icao: str) -> dict[int, dict[str, float]]:
    """How fast pilots slow to their approach speed on final, by approach speed (approach.data.response)."""
    r = json.loads((REAL_DIR / airport_icao.lower() / "response.json").read_text())
    table = r["speed"].get("final_slowing_5_to_3_nm_kt_s", {})
    return {int(k): v for k, v in table.items() if k.isdigit()}


def draw(quantiles: dict[str, float], u: float, bounds: tuple[float, float]) -> float:
    """A value at cumulative probability u from the 10th-90th percentiles, linear between them and out to the
    bounds in the tails."""
    qs = [0.1, 0.25, 0.5, 0.75, 0.9]
    vs = [quantiles[f"p{int(q * 100)}"] for q in qs]
    lo = max(bounds[0], vs[0] - (vs[1] - vs[0]) * 0.1 / 0.15)
    hi = min(bounds[1], vs[-1] + (vs[-1] - vs[-2]) * 0.1 / 0.15)
    xs, ys = [0.0] + qs + [1.0], [lo] + vs + [hi]
    for k in range(len(xs) - 1):
        if u <= xs[k + 1]:
            f = (u - xs[k]) / (xs[k + 1] - xs[k])
            return ys[k] + f * (ys[k + 1] - ys[k])
    return hi


def _bluesky(sim_dt: float = SIM_DT_S):
    """Start BlueSky once per process (headless), or reset it for a new session, with the given time step."""
    global _BS_READY
    import bluesky as bs

    with contextlib.redirect_stdout(io.StringIO()):
        if not _BS_READY:
            bs.init(mode="sim", detached=True)
            _BS_READY = True
        bs.settings.simdt = sim_dt  # reset() reads it
        bs.sim.reset()
        from bluesky.core import simtime

        simtime.setdt(sim_dt)
        bs.sim.simdt = sim_dt
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
    expedite: bool = False  # told to expedite its descent, until it reaches its cleared level


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
    # How this pilot flies ("measured" pilots only; "bluesky" pilots leave it to BlueSky)
    bank_deg: float = 25.0
    descent_fpm: float = 1500.0  # the rate now (with descent_profile, at the aircraft's height)
    descent_profile: tuple[float, ...] = ()  # its rate at the middle of each measured height band
    descent_u: float = 0.5  # where its usual rate lies in the measured spread (its expedited rate lies there too)
    expedite_profile: tuple[float, ...] = ()  # its rate in each band once told to expedite
    drift_lasting: float = 0.0  # "drifting" pilots: the lasting part of its place in the spread (normal score)
    drift_z: float = 0.0  # ... and the part that drifts
    drift_t: float = 0.0  # ... last moved at this time
    expedite: bool = False  # expediting its descent now
    expedite_told: bool = False  # told to expedite (controllers plan with it at once, as with a new level)
    decel_kt_s: float = 0.0
    decel_desc_kt_s: float = 0.0
    accel_kt_s: float = 0.0
    final_slowing_kt_s: float = 0.0  # from 160 kt to its approach speed, on final
    spd_goal: float | None = None  # the speed it is changing to, at its own rate
    spd_set: float | None = None  # the speed currently set in the autopilot on the way there

    @property
    def wake(self) -> str:
        return wake.category(self.actype)

    @property
    def approach_speed(self) -> int:
        return wake.approach_speed(self.actype)


class World:
    def __init__(self, airport: Airport, runway: str, seed: int = 1, pilot_delay_s: tuple[float, float] = (3.0, 6.0),
                 wind: "WindProfile | None" = None, sim_dt: float | None = None, pilots: str = "bluesky"):
        if pilots not in PILOT_MODELS:
            raise ValueError(f"pilots must be one of {PILOT_MODELS}")
        self.bs = _bluesky(sim_dt or SIM_DT_S)
        from bluesky import stack

        self.stack = stack.stack
        self.airport = airport
        self.runway = airport.runways[runway]
        self.rng = random.Random(seed)
        self.pilot_model = pilots
        self.measured = pilots in ("measured", "drifting", "drifting_handover")  # pilots who fly like real arrivals
        self.drifting = pilots in ("drifting", "drifting_handover")
        # Their own generator, so measured pilots leave the response delays exactly as they were.
        self.pilot_rng = random.Random(seed * 7919 + 17)
        self.drift_rng = random.Random(seed * 104729 + 31)  # its own too, so the other draws stay as they were
        self.pilot_delay_s = pilot_delay_s
        self.pilots: dict[str, Pilot] = {}
        self._last_hdg: dict[str, float] = {}
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
        # The weather model's top level (about 13,400 ft) again high up: BlueSky has no wind above its top level,
        # so aircraft handed over at FL150 flew in calm air and met the full wind lower down (one, turning outbound
        # in a hold in calm air, then crabbed into the wind below). Above the top the wind stays as it is there,
        # as WindProfile.uv has it.
        top = max(WIND_CEILING_FT, wind.heights_ft[-1] + 1000.0)
        veast = np.array([np.array(g).ravel() for g in wind.u_kt + wind.u_kt[-1:]]) / KT_PER_MS  # (levels, points), m/s
        vnorth = np.array([np.array(g).ravel() for g in wind.v_kt + wind.v_kt[-1:]]) / KT_PER_MS
        alt_m = np.array(list(wind.heights_ft) + [top]) / FT_PER_M
        self.bs.traf.wind.clear()
        self.bs.traf.wind.addpointvne(lat.ravel(), lon.ravel(), vnorth, veast, alt_m)
        if self.bs.traf.wind.fe is None:
            raise RuntimeError("BlueSky could not build the wind field from the weather grid")
        self.wind = wind

    # ---- traffic -----------------------------------------------------------------
    def spawn(self, cs: str, actype: str, lat: float, lon: float, hdg_true: float, alt_ft: float, cas_kt: float,
              vs_fpm: float | None = None) -> None:
        """A new aircraft; ``vs_fpm`` is its real vertical rate at handover, if known (used by "drifting_handover"
        pilots only)."""
        if cs in self.pilots:
            # BlueSky keeps the aircraft already flying and the new pilot would take it over (once, a holding
            # aircraft lost its hold and flew off 139 NM): two aircraft in the air need two names.
            raise ValueError(f"{cs} is already in the air")
        self.stack(f"CRE {cs} {actype} {lat:.6f} {lon:.6f} {hdg_true:.1f} {alt_ft:.0f} {cas_kt:.0f}")
        self.bs.sim.step()  # process the command so the aircraft exists
        p = Pilot(cs, actype, self.t, hdg_true, alt_ft, cas_kt)
        if self.measured:
            q = response_quantiles(self.airport.icao)
            for name in _BOUNDS:
                u = self.pilot_rng.random()
                setattr(p, name, draw(q[name], u, _BOUNDS[name]))
                if name == "descent_fpm" and descent_by_height(self.airport.icao):
                    p.descent_u = u
                    p.descent_profile = descent_profile(self.airport.icao, u)
                    p.expedite_profile = expedite_profile(self.airport.icao, u)
                    p.descent_fpm = descent_rate_at(self.airport.icao, p.descent_profile, alt_ft)
                    if self.pilot_model == "drifting_handover" and vs_fpm is not None \
                            and -vs_fpm >= HANDOVER_DESCENT_FPM:
                        u = place_of(self.airport.icao, -vs_fpm, alt_ft)
                        p.descent_u = u
                        p.descent_profile = descent_profile(self.airport.icao, u)
                        p.expedite_profile = expedite_profile(self.airport.icao, u)
                        p.descent_fpm = descent_rate_at(self.airport.icao, p.descent_profile, alt_ft)
                    if self.drifting:
                        p.drift_lasting, p.drift_z = drift_start(p.descent_u, self.drift_rng)
                        p.drift_t = self.t
            table = final_slowing_quantiles(self.airport.icao)
            if table:
                nearest = min(table, key=lambda k: abs(k - p.approach_speed))
                p.final_slowing_kt_s = draw(table[nearest], self.pilot_rng.random(), FINAL_SLOWING_BOUNDS)
            i = self._idx(cs)
            if i >= 0:
                self.bs.traf.ap.bankdef[i] = math.radians(p.bank_deg)
                self.bs.traf.ap.vsdef[i] = p.descent_fpm * FPM_TO_MS
            p.spd_set = cas_kt
        self.pilots[cs] = p

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
        elif instr.kind == "expedite":
            pilot.expedite_told = True

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
                expedite=p.expedite_told,
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

    def _hdg(self, cs: str, hdg: float, force: bool = False) -> None:
        """Set an autopilot heading, skipping repeats: BlueSky does a wind calculation for every heading command."""
        hdg %= 360.0
        last = self._last_hdg.get(cs)
        if force or last is None or abs(heading_difference(hdg, last)) >= 0.5:
            self.stack(f"HDG {cs} {hdg:.1f}")
            self._last_hdg[cs] = hdg

    def _apply(self, p: Pilot, instr: Instruction) -> None:
        cs = p.cs
        if instr.kind == "heading":
            p.direct_fix = None
            p.hold_fix = None  # a heading takes it out of the hold
            if not p.loc:
                self._hdg(cs, float(instr.value), force=True)
        elif instr.kind == "altitude":
            if not p.gs:
                self.stack(f"ALT {cs} {float(instr.value):.0f}")
        elif instr.kind == "speed":
            self._speed(p, float(instr.value))
        elif instr.kind == "direct":
            p.direct_fix = str(instr.value)
            p.hold_fix = None
        elif instr.kind == "hold" and str(instr.value) in self.airport.holds:
            p.direct_fix = None
            p.hold_fix, p.hold_state = str(instr.value), "to_fix"
        elif instr.kind == "cleared_ils":
            p.cleared = str(instr.value)
        elif instr.kind == "cancel_approach" and not p.loc:
            p.cleared = None  # it keeps its heading and level, and won't capture the localiser until cleared again
        elif instr.kind == "expedite" and p.expedite_profile:
            p.expedite = True  # measured pilots only: "bluesky" pilots descend at BlueSky's own rates

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
            if p.spd_goal is not None:
                self._speed_tick(p, v)
            if self.measured:
                self._bank_tick(p, v)
                if p.descent_profile:
                    self._descent_tick(p, v)
            if p.hold_fix:
                self._hold_tick(p, v)
            elif p.direct_fix and p.direct_fix in self.airport.fixes:
                fx, fy = self.airport.xy(self.airport.fixes[p.direct_fix])
                self._hdg(cs, self.heading_for_track(v, bearing_xy(v.x, v.y, fx, fy)))
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

    def _descent_tick(self, p: Pilot, v: AircraftView) -> None:
        """The pilot's descent rate for its height (the autopilot's default rate for a change of level); faster
        while expediting, which lasts until it reaches the level it is cleared to."""
        if p.expedite_told and v.alt_ft <= p.target_alt + EXPEDITE_DONE_FT:
            p.expedite = p.expedite_told = False
        if self.drifting:
            while self.t - p.drift_t >= DRIFT_EVERY_S:
                p.drift_z, u = drift_step(p.drift_lasting, p.drift_z, DRIFT_EVERY_S, self.drift_rng)
                p.drift_t += DRIFT_EVERY_S
                if abs(u - p.descent_u) >= 0.01:
                    p.descent_u = u
                    p.descent_profile, p.expedite_profile = profiles_at(self.airport.icao, round(u, 3))
        profile = p.expedite_profile if p.expedite and p.expedite_profile else p.descent_profile
        rate = descent_rate_at(self.airport.icao, profile, v.alt_ft)
        if abs(rate - p.descent_fpm) >= 10.0:
            p.descent_fpm = rate
            i = self._idx(p.cs)
            if i >= 0:
                self.bs.traf.ap.vsdef[i] = rate * FPM_TO_MS

    def _bank_tick(self, p: Pilot, v: AircraftView) -> None:
        """A measured pilot's autopilot banks in proportion to the heading still to turn, up to the pilot's limit."""
        target = self._last_hdg.get(p.cs)
        if target is None:
            return
        bank = max(MIN_BANK_DEG, min(p.bank_deg, BANK_PER_DEG * abs(heading_difference(target, v.hdg))))
        i = self._idx(p.cs)
        if i >= 0:
            self.bs.traf.ap.bankdef[i] = math.radians(bank)

    def _speed(self, p: Pilot, kt: float) -> None:
        """Set a new speed: at once for BlueSky's pilots, at the pilot's own rate for measured ones."""
        if self.measured:
            p.spd_goal = kt
        else:
            self.stack(f"SPD {p.cs} {kt:.0f}")

    def _speed_tick(self, p: Pilot, v: AircraftView) -> None:
        """A measured pilot moves the autopilot's speed towards the new one a little each second: real arrivals
        slow down at about 0.6 kt a second in level flight and 0.45 while descending, far slower than BlueSky."""
        goal = p.spd_goal
        now = p.spd_set if p.spd_set is not None else v.cas_kt
        if goal > now:
            rate = p.accel_kt_s
        elif p.gs and goal <= p.approach_speed and p.final_slowing_kt_s > 0:
            rate = p.final_slowing_kt_s  # gear and flaps: its own rate down to its approach speed
        else:
            rate = p.decel_desc_kt_s if v.vs_fpm < -300 else p.decel_kt_s
        step = max(-rate, min(rate, goal - now))
        p.spd_set = goal if abs(goal - now) <= rate else now + step
        if p.spd_set == goal:
            p.spd_goal = None
        self.stack(f"SPD {p.cs} {p.spd_set:.1f}")

    def _hold_tick(self, p: Pilot, v: AircraftView) -> None:
        """Fly the published racetrack: to the fix, turn outbound, a timed leg, turn back, inbound to the fix.

        A turn is finished when the heading or the track is near the one wanted: BlueSky's autopilot keeps the
        track a heading command gave in the wind at that moment, so an aircraft that descends into wind after its
        turn crabs, and its heading can stay several degrees off for good. Waiting for the heading alone left a
        pilot on its outbound turn for 18 minutes, 85 NM from the fix. HOLD_TURN_MAX_S ends a turn that still
        hasn't finished."""
        hold = self.airport.holds[p.hold_fix]
        fx, fy = self.airport.xy(hold.position)
        inbound = self.airport.magnetic_to_true(hold.inbound_course_mag)
        outbound = (inbound + 180.0) % 360.0
        side = 1.0 if hold.turn == "right" else -1.0
        leg_s = 60.0 if v.alt_ft <= 14000 else 90.0
        dist = math.hypot(fx - v.x, fy - v.y)

        def near(course: float, tolerance: float) -> bool:
            return min(abs(heading_difference(v.hdg, course)), abs(heading_difference(v.trk, course))) < tolerance

        if p.hold_state in ("to_fix", "inbound"):
            self._hdg(p.cs, self.heading_for_track(v, bearing_xy(v.x, v.y, fx, fy)))
            if dist < 0.7:
                p.hold_state, p.hold_t = "turn_out", self.t
                self._hdg(p.cs, (inbound + 90.0 * side) % 360)  # turn the published way
        elif p.hold_state == "turn_out":
            mid = (inbound + 90.0 * side) % 360
            late = self.t - p.hold_t >= HOLD_TURN_MAX_S["turn_out"]
            if near(mid, 15) or late:
                self._hdg(p.cs, outbound)
            if near(outbound, 5) or late:
                p.hold_state, p.hold_t = "outbound", self.t
        elif p.hold_state == "outbound":
            if self.t - p.hold_t >= leg_s:
                p.hold_state, p.hold_t = "turn_in", self.t
                self._hdg(p.cs, (outbound + 90.0 * side) % 360)
        elif p.hold_state == "turn_in":
            mid = (outbound + 90.0 * side) % 360
            if near(mid, 15) or self.t - p.hold_t >= HOLD_TURN_MAX_S["turn_in"]:
                p.hold_state = "inbound"

    @staticmethod
    def heading_for_track(v: AircraftView, track: float) -> float:
        """The heading that makes good the given track in the wind at the aircraft."""
        tr = math.radians(track)
        cross = v.wind_e_kt * math.cos(tr) - v.wind_n_kt * math.sin(tr)  # wind component to the right of the track
        ratio = max(-0.9, min(0.9, cross / max(v.tas_kt, 100.0)))
        return (track - math.degrees(math.asin(ratio))) % 360.0

    def _turn_radius_nm(self, tas_kt: float, p: Pilot | None = None) -> float:
        if self.measured and p is not None:  # radius = V^2 / (g tan(bank))
            v_ms = max(tas_kt, 100.0) * 0.514444
            return v_ms ** 2 / (9.80665 * math.tan(math.radians(p.bank_deg))) / 1852.0
        ref_radius = (TURN_RATE_REF_TAS_KT / 3600.0) / math.radians(TURN_RATE_REF_DEG_S)
        return ref_radius * (max(tas_kt, 100.0) / TURN_RATE_REF_TAS_KT) ** 2

    def _approach_tick(self, p: Pilot, v: AircraftView) -> list[dict]:
        rwy = self.runway
        c = rwy.course_true
        angle = heading_difference(v.trk, c)  # + means tracking right of the localiser course
        events: list[dict] = []
        if not p.loc:
            converging = (v.lateral < 0 < angle) or (v.lateral > 0 > angle) or abs(v.lateral) < 0.05
            lead = self._turn_radius_nm(max(v.tas_kt, v.gs_kt), p) * (1 - math.cos(math.radians(abs(angle)))) + 0.05
            # Autopilots capture at steep intercepts too (the wind can make a 30 degree intercept heading a much
            # steeper track); they just overshoot more.
            if converging and abs(angle) <= MAX_CAPTURE_TRACK_DEG and abs(v.lateral) <= lead and v.along > 1.0:
                p.loc = True
                events.append({"kind": "pilot", "cs": p.cs, "text": f"{p.cs} localiser captured {v.along:.1f} NM out"})
        if p.loc:
            correction = max(-30.0, min(30.0, v.lateral * 60.0))
            self._hdg(p.cs, self.heading_for_track(v, (c - correction) % 360))
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
                self._speed(p, 160)
            if v.along < APPROACH_SPEED_FROM_NM[self.pilot_model] and p.target_spd > p.approach_speed:
                p.target_spd = p.approach_speed
                self._speed(p, p.approach_speed)
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
        self._hdg(p.cs, self.runway.course_true)
        self.stack(f"VS {p.cs} 2000")
        self.stack(f"ALT {p.cs} 3000")
        self._speed(p, 200)
        return [{"kind": "go_around", "cs": p.cs, "reason": reason, "text": f"{p.cs} going around: {reason}"}]

    def _go_around_tick(self, p: Pilot, v: AircraftView) -> list[dict]:
        # Published missed approach: straight ahead to 3,000 ft, then heading 178 (magnetic)
        # after passing 2,000 ft or 1 DME, whichever is later.
        if p.go_around_stage == 1 and v.alt_ft >= 2000 and v.along < 1.0:
            p.go_around_stage = 2
            p.target_hdg = self.airport.magnetic_to_true(178)
            self._hdg(p.cs, p.target_hdg)
        if p.go_around_stage == 2 and v.alt_ft >= 2800:
            p.phase, p.go_around_stage = "vectoring", 0
            p.stable_checked = False
            return [{"kind": "pilot", "cs": p.cs, "text": f"{p.cs} back with the final director at 3,000 ft"}]
        return []
