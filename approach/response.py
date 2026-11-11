"""How aircraft respond to an instruction: draws of their whole next two minutes.

Stage 2's first step. Given what a controller sees of each aircraft and the clearance it is flying, the model
predicts where it will be every 5 seconds for the next 2 minutes, with or without a new heading, level or speed.
It doesn't give one guess. Each draw is a whole future flown by its own pilot, with a response delay, a bank
angle, a descent rate and rates of slowing down and speeding up drawn from what real Gatwick arrivals do
(approach.data.response, the same measurements the simulated pilots use). How far the draws spread is how sure
the prediction is.

The physics is simple and the same in every draw. The aircraft turns towards its heading at the rate its bank
gives at its true airspeed (banking in proportion to the heading still to turn, up to its pilot's limit), changes its indicated airspeed at its pilot's rate, climbs or descends at its
pilot's rate, flies through the air on its heading and drifts with the wind. Cleared for the ILS, it captures
the localiser as the simulated pilots do, holds the centreline, follows the glide path down and slows to 160 kt
by 7 NM and its approach speed by 4 NM. A holding aircraft stays where it is (it circles its fix).

It serves the safety check's look-ahead and the AI's inputs first, and model-based planning later. It is tested
against constant velocity and the safety check's old look-ahead, in the simulator and on real tracks from the
test days (scripts/response_check.py).
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass

import numpy as np

from approach import wake
from approach.airports.model import Airport
from approach.data.response import G_KT_DEG, tas_from_cas
from approach.sim.world import (_BOUNDS, BANK_PER_DEG, MAX_CAPTURE_TRACK_DEG, MIN_BANK_DEG, draw,
                                response_quantiles)

HORIZON_S = 120
OUT_STEP_S = 5
DT_S = 2.5
PILOT_DELAY_S = (3.0, 6.0)  # the simulated pilots' (real response delays are not in the ADS-B data)
FINAL_SPEED_KT, FINAL_SPEED_NM, APPROACH_SPEED_NM = 160.0, 7.0, 4.0


@dataclass
class State:
    """What a controller sees of each aircraft, one entry per aircraft (arrays of equal length)."""

    x: np.ndarray
    y: np.ndarray
    alt: np.ndarray
    cas: np.ndarray
    tas: np.ndarray
    hdg: np.ndarray
    wind_e: np.ndarray
    wind_n: np.ndarray
    target_hdg: np.ndarray
    target_alt: np.ndarray
    target_spd: np.ndarray
    approach_kt: np.ndarray
    cleared: np.ndarray  # cleared for the ILS
    loc: np.ndarray  # on the localiser
    gs: np.ndarray  # on the glide path
    holding: np.ndarray

    def __len__(self) -> int:
        return len(self.x)


def state_from_views(views) -> State:
    """The State of every aircraft a controller can see (approach.sim.world.AircraftView)."""
    f = lambda attr: np.array([float(getattr(v, attr)) for v in views])  # noqa: E731
    return State(
        x=f("x"), y=f("y"), alt=f("alt_ft"), cas=f("cas_kt"), tas=f("tas_kt"), hdg=f("hdg"),
        wind_e=f("wind_e_kt"), wind_n=f("wind_n_kt"), target_hdg=f("target_hdg"), target_alt=f("target_alt"),
        target_spd=f("target_spd"), approach_kt=np.array([float(wake.approach_speed(v.actype)) for v in views]),
        cleared=np.array([bool(v.cleared) for v in views]), loc=np.array([bool(v.loc) for v in views]),
        gs=np.array([bool(v.gs) for v in views]), holding=np.array([v.phase == "holding" for v in views]),
    )


@dataclass
class Pilots:
    """One pilot per draw (arrays of equal length)."""

    delay_s: np.ndarray
    bank_deg: np.ndarray
    descent_fpm: np.ndarray
    decel_kt_s: np.ndarray
    decel_desc_kt_s: np.ndarray
    accel_kt_s: np.ndarray
    # How far the real wind is from the forecast at the aircraft, in this draw (kt, towards east and north)
    wind_e_err: np.ndarray | None = None
    wind_n_err: np.ndarray | None = None
    # How far from its assigned speed this draw's pilot settles (kt)
    speed_err: np.ndarray | None = None

    def __len__(self) -> int:
        return len(self.delay_s)


def draw_pilots(n: int, airport_icao: str, rng: random.Random | None = None,
                delay_s: tuple[float, float] = PILOT_DELAY_S, wind_sd_kt: float = 0.0,
                speed_sd_kt: float = 0.0) -> Pilots:
    """n pilots, each quantity spread evenly over its measured distribution (one draw in each of n equal slices of
    probability, shuffled independently), so a few dozen draws already cover the tails. ``wind_sd_kt`` adds a
    wind error to each draw, for when the wind at the aircraft is a forecast (real tracks, the live feed); in the
    simulator the wind is known. ``speed_sd_kt`` lets each draw's pilot settle a little off its assigned speed."""
    rng = rng or random.Random(0)
    q = response_quantiles(airport_icao)

    def spread(fn) -> np.ndarray:
        us = [(k + rng.random()) / n for k in range(n)]
        rng.shuffle(us)
        return np.array([fn(u) for u in us])

    values = {name: spread(lambda u, name=name: draw(q[name], u, _BOUNDS[name])) for name in _BOUNDS}
    gauss = lambda sd: np.array([rng.gauss(0.0, sd) for _ in range(n)]) if sd > 0 else None  # noqa: E731
    return Pilots(delay_s=spread(lambda u: delay_s[0] + u * (delay_s[1] - delay_s[0])), **values,
                  wind_e_err=gauss(wind_sd_kt), wind_n_err=gauss(wind_sd_kt), speed_err=gauss(speed_sd_kt))


def median_pilot(airport_icao: str, delay_s: float = sum(PILOT_DELAY_S) / 2) -> Pilots:
    """The typical pilot: every quantity at its median. One draw, for a single best guess."""
    q = response_quantiles(airport_icao)
    return Pilots(delay_s=np.array([delay_s]), **{name: np.array([q[name]["p50"]]) for name in _BOUNDS})


def _wrap(deg: np.ndarray) -> np.ndarray:
    return (deg + 180.0) % 360.0 - 180.0


class ResponseModel:
    def __init__(self, airport: Airport, runway: str, horizon_s: int = HORIZON_S, out_step_s: int = OUT_STEP_S,
                 dt_s: float = DT_S):
        self.airport = airport
        self.runway = airport.runways[runway]
        self.tx, self.ty = airport.xy(self.runway.threshold)
        c = math.radians(self.runway.course_true)
        self.ux, self.uy = math.sin(c), math.cos(c)
        self.course = self.runway.course_true
        self.slope_ft_per_nm = 6076.12 * math.tan(math.radians(self.runway.glide_slope_deg))
        self.gp0 = self.runway.elevation_ft + self.runway.reference_height_ft
        self.horizon_s, self.out_step_s, self.dt = horizon_s, out_step_s, dt_s
        self.steps = horizon_s // out_step_s
        self.sub = max(1, round(out_step_s / dt_s))

    def _frame(self, x: np.ndarray, y: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        dx, dy = x - self.tx, y - self.ty
        return -(dx * self.ux + dy * self.uy), dx * self.uy - dy * self.ux

    def predict(self, s: State, pilots: Pilots, new: tuple[int, str, float] | None = None) -> np.ndarray:
        """Positions every out_step_s over the horizon: an array (draws, aircraft, steps, 3) of x, y (NM) and
        altitude (ft). ``new`` is an instruction just given, (aircraft index, "heading" | "altitude" | "speed",
        value): each draw's pilot acts on it after its own delay; until then the aircraft flies its clearance."""
        D, M, dt = len(pilots), len(s), self.dt
        full = lambda a: np.broadcast_to(np.asarray(a, dtype=float), (D, M)).copy()  # noqa: E731
        x, y, alt, cas, hdg = full(s.x), full(s.y), full(s.alt), full(s.cas), full(s.hdg)
        thdg, talt, tspd = full(s.target_hdg), full(s.target_alt), full(s.target_spd)
        speed_err = 0.0 if pilots.speed_err is None else pilots.speed_err[:, None]
        loc, gsp = full(s.loc) > 0.5, full(s.gs) > 0.5
        cleared, hold = full(s.cleared) > 0.5, full(s.holding) > 0.5
        we, wn, appr = full(s.wind_e), full(s.wind_n), full(s.approach_kt)
        if pilots.wind_e_err is not None:
            we, wn = we + pilots.wind_e_err[:, None], wn + pilots.wind_n_err[:, None]
        # The true airspeed for an indicated airspeed, matched to what the aircraft shows now (temperature).
        tas_k = full(np.asarray(s.tas) / np.maximum(tas_from_cas(np.asarray(s.cas), np.asarray(s.alt)), 1.0))
        bank_max = pilots.bank_deg[:, None]
        tan_bank = np.tan(np.radians(bank_max))
        desc = (pilots.descent_fpm / 60.0)[:, None]
        decel, decel_d, accel = (pilots.decel_kt_s[:, None], pilots.decel_desc_kt_s[:, None],
                                 pilots.accel_kt_s[:, None])
        applied = np.zeros(D, dtype=bool)
        out = np.empty((D, M, self.steps, 3))
        t = 0.0
        for j in range(self.steps):
            for _ in range(self.sub):
                t += dt
                if new is not None:
                    now = ~applied & (t >= pilots.delay_s)
                    if now.any():
                        i, kind, value = new
                        target = {"heading": thdg, "altitude": talt, "speed": tspd}[kind]
                        target[now, i] = float(value)
                        applied |= now
                along, lateral = self._frame(x, y)
                # Speed: towards the assigned speed at the pilot's own rate (slower while descending).
                slow = np.where(alt > talt + 100, decel_d, decel)
                cas += np.clip(tspd + speed_err - cas, -slow * dt, accel * dt)
                tas = np.maximum(tas_from_cas(cas, alt) * tas_k, 80.0)
                # On the localiser the pilot steers to the centreline, correcting for the wind.
                if loc.any():
                    track = self.course - np.clip(lateral * 60.0, -30.0, 30.0)
                    tr = np.radians(track)
                    cross = we * np.cos(tr) - wn * np.sin(tr)
                    on_loc = track - np.degrees(np.arcsin(np.clip(cross / np.maximum(tas, 100.0), -0.9, 0.9)))
                    thdg = np.where(loc, on_loc % 360.0, thdg)
                # Heading: at the rate of turn its bank gives at this true airspeed.
                to_turn = _wrap(thdg - hdg)
                bank = np.clip(BANK_PER_DEG * np.abs(to_turn), MIN_BANK_DEG, bank_max)
                rate = G_KT_DEG * np.tan(np.radians(bank)) / np.maximum(tas, 100.0)
                turn = np.clip(to_turn, -rate * dt, rate * dt)
                mid = np.radians(hdg + turn / 2.0)
                hdg = (hdg + turn) % 360.0
                vx = tas * np.sin(mid) + we
                vy = tas * np.cos(mid) + wn
                moving = ~hold
                x = np.where(moving, x + vx * dt / 3600.0, x)
                y = np.where(moving, y + vy * dt / 3600.0, y)
                # Height: to the cleared level at the pilot's rate (climbs too); the glide path once on it.
                alt = np.where(moving, alt + np.clip(talt - alt, -desc * dt, desc * dt), alt)
                along, lateral = self._frame(x, y)
                gp = self.gp0 + np.maximum(along, 0.0) * self.slope_ft_per_nm
                if cleared.any():
                    angle = _wrap(np.degrees(np.arctan2(vx, vy)) - self.course)
                    radius = (tas * 0.514444) ** 2 / (9.80665 * tan_bank) / 1852.0
                    lead = radius * (1 - np.cos(np.radians(np.abs(angle)))) + 0.05
                    converging = ((lateral < 0) & (angle > 0)) | ((lateral > 0) & (angle < 0)) | (np.abs(lateral) < 0.05)
                    capture = (cleared & ~loc & converging & (np.abs(angle) <= MAX_CAPTURE_TRACK_DEG)
                               & (np.abs(lateral) <= lead) & (along > 1.0))
                    loc |= capture
                    gsp |= loc & (alt >= gp - 30.0)
                    alt = np.where(gsp, np.maximum(gp, self.runway.elevation_ft), alt)
                    tspd = np.where(loc & (along < FINAL_SPEED_NM), np.minimum(tspd, FINAL_SPEED_KT), tspd)
                    tspd = np.where(loc & (along < APPROACH_SPEED_NM), np.minimum(tspd, appr), tspd)
            out[:, :, j, 0], out[:, :, j, 1], out[:, :, j, 2] = x, y, alt
        return out


def constant_velocity(s: State, gs_e: np.ndarray, gs_n: np.ndarray, vs_fpm: np.ndarray,
                      horizon_s: int = HORIZON_S, out_step_s: int = OUT_STEP_S) -> np.ndarray:
    """The simplest guess: every aircraft keeps its ground velocity and vertical speed. (1, aircraft, steps, 3)."""
    t = np.arange(out_step_s, horizon_s + 1, out_step_s, dtype=float)[None, :]
    x = s.x[:, None] + gs_e[:, None] * t / 3600.0
    y = s.y[:, None] + gs_n[:, None] * t / 3600.0
    alt = s.alt[:, None] + vs_fpm[:, None] * t / 60.0
    return np.stack([x, y, alt], axis=-1)[None]


def crps(draws: np.ndarray, truth: np.ndarray) -> np.ndarray:
    """The continuous ranked probability score of an ensemble, along the first axis: how far the draws are from the
    truth, less half how far they are from each other. For a single draw it is the absolute error. Lower is better."""
    d = draws.shape[0]
    term1 = np.mean(np.abs(draws - truth[None]), axis=0)
    if d == 1:
        return term1
    s = np.sort(draws, axis=0)
    k = np.arange(1, d + 1).reshape((d,) + (1,) * (draws.ndim - 1))
    term2 = np.sum((2 * k - d - 1) * s, axis=0) / (d * d)  # = 0.5 * E|X - X'|
    return term1 - term2
