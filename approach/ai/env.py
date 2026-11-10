"""Stage 1a's training world: the AI sets speeds, the rules controller flies fixed paths.

Each episode is a real hour of Gatwick arrivals from the training days (the test days are
never used here), starting at a random minute, in real winds (usually that hour's, sometimes
another hour's), with pilots whose delays and drift come from a fresh seed: never the same
session twice. The rules controller keeps the routing, altitudes, landing order and ILS
clearances on fixed paths (every arrival joins final at 11 NM, downwinds south of the
centreline), and the AI sets the speeds.

Every 10 seconds the AI looks at the aircraft in the landing sequence, in landing order, and
gives each a speed, as a controller says it ("speed 180"): 160 to 250 kt in 10 kt values,
inside the published band for its stage (approach.controllers.speed). Giving its current
speed leaves it alone. Every instruction goes through the safety check. It sees each aircraft in relative terms: track miles to go, the gap
to the aircraft ahead and how fast it is closing, the gap it needs, the gap behind, its wake
category, its height against the 3-to-1 profile, its margins, its position measured from the
runway, and the wind along the runway.

What the AI sees (`observe`) and how its choices become instructions (`speed_instructions`)
are shared with AISpeedController, which flies a trained AI in ordinary sessions, so replays
and the benchmark run exactly what was trained. `teacher_action` is the speed-only rules
controller's choice in the same terms, for the head start (behaviour cloning) and the
comparison. The score (REWARD) has a weight for every term so each run can record them.
"""

from __future__ import annotations

import csv
import math
import random
from datetime import date, datetime, timezone

import numpy as np

try:
    import gymnasium as gym
    from gymnasium import spaces
except ImportError:  # the simulator itself doesn't need gymnasium
    gym = None

from approach import wake
from approach.airports import egkk
from approach.controllers import speed
from approach.controllers.base import Controller
from approach.controllers.rules import INTERCEPT_LEG_NM, RulesController
from approach.data.arrivals import REAL_DIR, load_day
from approach.geo import distance_xy
from approach.instructions import Instruction
from approach.session import SessionLog
from approach.sim.live import LiveSession

N_MAX = 16  # aircraft in the sequence the AI can see and act on at once
FEATURES = (
    "present", "controllable", "downwind", "base", "cleared", "track_miles", "time_to_go", "assigned_kt", "cas_kt",
    "gs_kt", "gap_ahead_nm", "has_ahead", "gap_needed_nm", "closing_kt", "gap_behind_nm", "has_behind",
    "wake_J", "wake_H", "wake_U", "wake_M", "wake_S", "wake_L", "above_profile_ft", "above_minimum_ft",
    "inside_rma_nm", "along_nm", "lateral_nm", "headwind_kt", "band_lo_kt", "band_hi_kt",
)
N_FEATURES = len(FEATURES)
TARGET_BUFFER_NM = 0.5  # the gap aimed for: the minimum plus this
MAX_SKIP = 2  # extra 10 s spells a step may skip when no aircraft is in the sequence
MAX_TAILWIND_KT = 15.0  # another hour's wind is used only if its tailwind on the runway is no more than this


def tailwind_kt(wind, airport, runway: str, heights_ft: tuple[float, ...] = (1000.0, 2000.0, 3000.0)) -> float:
    """The strongest tailwind (negative: headwind) along a runway's landing direction, near its threshold, over the
    heights of the final approach."""
    rwy = airport.runways[runway]
    course = math.radians(rwy.course_true)
    return max(u * math.sin(course) + v * math.cos(course)
               for u, v in (wind.uv(rwy.threshold.lat, rwy.threshold.lon, h) for h in heights_ft))

REWARD = {
    "landing": 1.0,  # each aircraft landed
    "gap_too_close_per_nm": 2.0,  # at each landing: the follower inside the target gap, per NM short
    "gap_too_far_per_nm": 0.2,  # ... or beyond it, per NM long (capped at 4 NM)
    "on_target": 0.005,  # every 10 s, each gap within 0.5 NM of its target
    "on_profile": 0.002,  # every 10 s, each aircraft within 1,000 ft of the 3-to-1 profile
    "instruction": 0.01,  # each speed change the AI chooses
    "per_aircraft_minute": 0.01,  # time in the sector
    "blocked": 0.5,  # an instruction the safety check blocked
    "go_around": 3.0,
    "separation_loss": 5.0,  # a pair coming inside the radar minimum
    "wake_infringement": 3.0,  # a pair on final coming inside the wake minimum
}


def training_hours(split: str = "train", min_arrivals: int = 3) -> list[tuple[str, int]]:
    """(day, hour) for every hour of real traffic on the chosen days of a split (or several, comma-separated)."""
    splits = set(split.split(","))
    rows = csv.DictReader((REAL_DIR / "egkk" / "real_controller_hours.csv").open())
    return [(r["day"], int(r["hour"])) for r in rows if r["split"] in splits and int(r["arrivals"]) >= min_arrivals]


def winds_cached(day: str, hour: int) -> bool:
    from approach.data.gfs import CACHE_DIR

    return (CACHE_DIR / f"egkk_{day.replace('-', '')}{hour:02d}.json").is_file()


# ---- what the AI sees, and what its choices become (shared by training and AISpeedController) ----
def track_miles(controller: RulesController, v) -> float:
    """Distance to fly to touchdown on the aircraft's planned path."""
    p = controller.plans.get(v.cs)
    if p is None:
        return max(v.along, 0.0)
    pts = controller._points(p)
    if p.stage == "downwind":
        return distance_xy(v.x, v.y, *pts["D"]) + distance_xy(*pts["D"], *pts["B"]) + INTERCEPT_LEG_NM + p.d_join
    if p.stage == "base":
        return distance_xy(v.x, v.y, *pts["B"]) + INTERCEPT_LEG_NM + p.d_join
    if v.loc:
        return max(v.along, 0.0)
    return distance_xy(v.x, v.y, *pts["J"]) + p.d_join


def observe(views: list, controller: RulesController, t: float) -> tuple[np.ndarray, list[str]]:
    """The AI's picture: one row per aircraft in the landing sequence, in landing order; empty rows after."""
    from shapely.geometry import Point

    airport, runway, rma = controller.airport, controller.runway, controller.rma
    by_cs = {v.cs: v for v in views}
    plans = [p for p in controller.plans.values() if p.cs in by_cs and p.stage in ("downwind", "base", "cleared")]
    plans.sort(key=lambda p: (p.slot_t or p.landing_t))
    slots = [p.cs for p in plans][:N_MAX]
    obs = np.zeros((N_MAX, N_FEATURES), dtype=np.float32)
    tm = {cs: track_miles(controller, by_cs[cs]) for cs in slots}
    course = math.radians(runway.course_true)
    for i, cs in enumerate(slots):
        v, p = by_cs[cs], controller.plans[cs]
        limits = speed.band(p.stage, v.along)
        ahead = slots[i - 1] if i > 0 else None
        behind = slots[i + 1] if i + 1 < len(slots) else None
        need = max(wake.RADAR_MINIMUM_NM, wake.separation_nm(by_cs[ahead].actype, v.actype)) if ahead else 3.0
        cat = wake.category(v.actype)
        minimum = airport.minimum_altitude_ft(v.x, v.y) or 0.0
        inside = 0.0
        if rma is not None:
            d = rma.polygon.exterior.distance(Point(v.x, v.y))
            inside = d if rma.contains(v.x, v.y) else -d
        headwind = -(v.wind_e_kt * math.sin(course) + v.wind_n_kt * math.cos(course))
        row = {
            "present": 1.0, "controllable": 1.0 if limits else 0.0,
            "downwind": float(p.stage == "downwind"), "base": float(p.stage == "base"),
            "cleared": float(p.stage == "cleared"),
            "track_miles": tm[cs] / 30.0, "time_to_go": max(p.landing_t - t, 0.0) / 900.0,
            "assigned_kt": v.target_spd / 250.0, "cas_kt": v.cas_kt / 250.0, "gs_kt": v.gs_kt / 250.0,
            "gap_ahead_nm": (tm[cs] - tm[ahead]) / 10.0 if ahead else 1.0, "has_ahead": float(ahead is not None),
            "gap_needed_nm": (need + TARGET_BUFFER_NM) / 10.0,
            "closing_kt": (v.gs_kt - by_cs[ahead].gs_kt) / 60.0 if ahead else 0.0,
            "gap_behind_nm": (tm[behind] - tm[cs]) / 10.0 if behind else 1.0, "has_behind": float(behind is not None),
            **{f"wake_{c}": float(cat == c) for c in wake.CATEGORIES},
            "above_profile_ft": (v.alt_ft - tm[cs] * 1000.0 / 3.0) / 3000.0,
            "above_minimum_ft": (v.alt_ft - minimum) / 3000.0,
            "inside_rma_nm": inside / 10.0, "along_nm": v.along / 30.0, "lateral_nm": v.lateral / 10.0,
            "headwind_kt": headwind / 40.0,
            "band_lo_kt": (limits[0] if limits else 0) / 250.0, "band_hi_kt": (limits[1] if limits else 0) / 250.0,
        }
        obs[i] = [row[f] for f in FEATURES]
    return np.clip(obs, -5.0, 5.0), slots


def speed_instructions(action, slots: list[str], views: list, controller: RulesController) -> list[tuple[Instruction, bool]]:
    """Speed instructions from the AI's choices, as (instruction, chosen by the AI rather than a band edge).

    An aircraft whose speed is outside its band (on entering a new stage) is brought inside it
    whatever the choice, and that doesn't count as the AI's instruction.

    Aircraft not yet in the sequence (inbound or holding) are given the standard speed for
    their stage if they are outside its band.
    """
    by_cs = {v.cs: v for v in views}
    out = []
    for i, cs in enumerate(slots):
        v, stage = by_cs.get(cs), controller.stage_of(cs)
        limits = speed.band(stage, v.along) if v and stage else None
        if limits is None:
            continue
        new = speed.in_band(speed.SPEEDS_KT[int(action[i])], limits)
        if abs(new - v.target_spd) >= 5:
            procedural = not limits[0] <= v.target_spd <= limits[1]
            out.append((Instruction(cs, "speed", new), not procedural))
    for cs, v in by_cs.items():
        if cs in slots or controller.stage_of(cs) != "inbound":
            continue
        limits = speed.band("inbound", v.along)
        if limits and not limits[0] <= v.target_spd <= limits[1]:
            out.append((Instruction(cs, "speed", speed.STANDARD_KT["inbound"]), False))
    return out


def teacher_action(slots: list[str], views: list, controller: RulesController) -> np.ndarray:
    """The speed-only rules controller's choice for each slot, as the AI's actions.

    Empty slots and aircraft whose speed isn't the controller's to give get their current speed.
    """
    by_cs = {v.cs: v for v in views}
    action = np.full(N_MAX, speed.nearest_choice(speed.STANDARD_KT["downwind"]), dtype=np.int64)
    for i, cs in enumerate(slots):
        p, v = controller.plans.get(cs), by_cs.get(cs)
        if p is None or v is None:
            continue
        target = controller.slot_target(p, v)
        action[i] = speed.nearest_choice(target if target is not None else v.target_spd)
    return action


class AISpeedController(Controller):
    """Stage 1a in an ordinary session: the rules controller on fixed paths, with a policy setting speeds.

    `policy` maps the AI's picture (observe) to its choices. With policy=None the speed-only
    rules controller's choices are used (teacher_action), which is how the comparison runs.
    """

    name = "ai_speed"

    def __init__(self, airport, runway: str, policy=None, name: str = "ai_speed"):
        self.rules = RulesController(airport, runway, fixed_paths=True, speeds="none")
        self.policy = policy
        self.name = name
        self.last_t = -1e9

    @property
    def plans(self):
        return self.rules.plans

    def decide(self, t: float, views: list) -> list[Instruction]:
        out = self.rules.decide(t, views)
        if t - self.last_t >= speed.DECISION_S:
            self.last_t = t
            obs, slots = observe(views, self.rules, t)
            action = self.policy(obs) if self.policy is not None else teacher_action(slots, views, self.rules)
            out += [instr for instr, _ in speed_instructions(action, slots, views, self.rules)]
        return out

    def on_event(self, event: dict) -> None:
        self.rules.on_event(event)


def load_policy(path: str):
    """A trained AI saved by approach.ai.train, as a function from the AI's picture to its choices."""
    from stable_baselines3 import PPO

    model = PPO.load(path, device="cpu")
    return lambda obs: model.predict(obs, deterministic=True)[0]


# ---- the training world ----------------------------------------------------------------------
class SpeedEnv(gym.Env if gym else object):
    """One episode = one real hour of arrivals; one step = 10 seconds."""

    metadata = {"render_modes": []}

    def __init__(self, split: str = "train", window_s: int = 3600, other_wind_p: float = 0.25, seed: int | None = None,
                 hours: list[tuple[str, int]] | None = None, reward: dict | None = None, frames: bool = True,
                 cached_winds_only: bool = False):
        if gym is None:
            raise ImportError("pip install gymnasium")
        self.airport = egkk.load()
        self.all_hours = hours or training_hours(split)
        self.window_s = window_s
        self.other_wind_p = other_wind_p
        self.reward_w = {**REWARD, **(reward or {})}
        self.frames = frames
        self.cached_winds_only = cached_winds_only
        self.rng = random.Random(seed)
        self._days: dict[str, dict] = {}
        self.observation_space = spaces.Box(-5.0, 5.0, shape=(N_MAX, N_FEATURES), dtype=np.float32)
        self.action_space = spaces.MultiDiscrete([len(speed.SPEEDS_KT)] * N_MAX)
        self.session: LiveSession | None = None
        self.slots: list[str] = []

    @property
    def hours(self) -> list[tuple[str, int]]:
        """The hours to draw from: while winds are still downloading, optionally only those already here."""
        if not self.cached_winds_only:
            return self.all_hours
        ready = [h for h in self.all_hours if winds_cached(*h)]
        return ready or self.all_hours

    def _day(self, d: str) -> dict:
        if d not in self._days:
            if len(self._days) > 6:
                self._days.pop(next(iter(self._days)))
            self._days[d] = load_day(date.fromisoformat(d), "EGKK")
        return self._days[d]

    def reset(self, seed: int | None = None, options: dict | None = None):
        super().reset(seed=seed)
        if seed is not None:
            self.rng.seed(seed)
        from approach.data import real_sessions
        from approach.data.gfs import fetch_for_airport, parse_time

        options = options or {}
        hours = self.hours
        for _ in range(20):  # skip windows with no arrivals
            d, h = options.get("hour") or self.rng.choice(hours)
            day = self._day(d)
            midnight = datetime.fromisoformat(d).replace(tzinfo=timezone.utc).timestamp()
            start = midnight + h * 3600 + options.get("offset_s", self.rng.uniform(0, 3600))
            if real_sessions.window_arrivals(day, start, start + self.window_s):
                break
        scenario = real_sessions.real_scenario(day, self.airport, start, start + self.window_s, wind=False)
        wind_hour = options.get("wind_hour") or datetime.fromtimestamp(start + self.window_s / 2, tz=timezone.utc).strftime("%Y-%m-%dT%H")
        wind = None
        if "wind_hour" not in options and "hour" not in options and self.rng.random() < self.other_wind_p:
            # Another hour's wind, for variety, but only one the runway in use could really be landed in: a summer
            # day's traffic in a storm's tailwind (run stage1a-04: 2 landings, 30 go-arounds) teaches nothing.
            for _ in range(10):
                od, oh = self.rng.choice(hours)
                other = fetch_for_airport(parse_time(f"{od}T{oh:02d}"), self.airport)
                if tailwind_kt(other, self.airport, scenario.runway) <= MAX_TAILWIND_KT:
                    wind_hour, wind = f"{od}T{oh:02d}", other
                    break
        self.wind_hour = wind_hour
        wind = wind or fetch_for_airport(parse_time(wind_hour), self.airport)
        self.scheme = wake.scheme_for(d)
        self.teacher = bool(options.get("teacher"))
        self.controller = RulesController(self.airport, scenario.runway, fixed_paths=True, speeds="none")
        self.controller.name = "rules_speed" if self.teacher else "ai_speed"
        log = SessionLog(meta={"scenario": scenario.name, "runway": scenario.runway, "wind": wind_hour,
                               "controller": self.controller.name, "wake_scheme": self.scheme}, map={})
        seed_pilots = options.get("pilot_seed", self.rng.randrange(1 << 30))
        with wake.scheme(self.scheme):
            self.session = LiveSession(self.airport, scenario, self.controller, log, seed=seed_pilots, wind=wind,
                                       frames=self.frames)
        self.in_conflict: set[tuple[str, str]] = set()
        self.wake_flagged: set[tuple[str, str]] = set()
        self.gaps: list[tuple[float, float]] = []  # (gap, target) at each landing with a follower on final
        self.counts = {"landed": 0, "go_arounds": 0, "separation_losses": 0, "losses_at_stack": 0,
                       "wake_infringements": 0, "instructions": 0, "blocked": 0, "decisions": 0}
        with wake.scheme(self.scheme):
            obs = self._observe()
            while not self.slots and not self.session.done:  # nothing to control yet: let the traffic arrive
                self._advance(speed.DECISION_S)
                obs = self._observe()
        return obs, {"scenario": scenario.name, "wind": wind_hour}

    def teacher_action(self) -> np.ndarray:
        """What the speed-only rules controller would choose now."""
        return teacher_action(self.slots, self.session.world.views(), self.controller)

    def step(self, action):
        r = 0.0
        with wake.scheme(self.scheme):
            r += self._act(action)
            r_adv, _ = self._advance(speed.DECISION_S)
            r += r_adv
            obs = self._observe()
            for _ in range(MAX_SKIP):  # skip a little through quiet spells, but keep steps alike in cost:
                if self.slots or self.session.done:  # parallel training waits for the slowest simulation
                    break
                r_adv, _ = self._advance(speed.DECISION_S)
                r += r_adv
                obs = self._observe()
            r += self._shaping(obs)
            self.counts["decisions"] += 1
            terminated = self.session.done
            info = {}
            if terminated:
                log = self.session.finish()
                info = {"scores": log.scores, "counts": dict(self.counts), "gaps": list(self.gaps),
                        "scenario": log.meta["scenario"], "wind": self.wind_hour}
        return obs, float(r), terminated, False, info

    def _act(self, action) -> float:
        s, r = self.session, 0.0
        s.views = s.world.views()
        for instr, chosen in speed_instructions(action, self.slots, s.views, self.controller):
            ok, _ = s.instruct(instr, notify_controller=False)
            if chosen:
                self.counts["instructions"] += 1
                r -= self.reward_w["instruction"]
            if not ok:
                self.counts["blocked"] += 1
                r -= self.reward_w["blocked"]
        return r

    def _advance(self, seconds: float) -> tuple[float, list[dict]]:
        """Move the session on; reward landings, gaps at landing, go-arounds, conflicts and time."""
        s, w, r = self.session, self.reward_w, 0.0
        events = []
        for _ in range(int(seconds)):
            if s.done:
                break
            ticked = s.tick()
            before = {v.cs: v for v in s.views}  # where everyone was at the start of that second
            for e in ticked:
                events.append(e)
                if e["kind"] == "landed":
                    self.counts["landed"] += 1
                    r += w["landing"] + self._gap_at_landing(e["cs"], before)
                elif e["kind"] == "go_around":
                    self.counts["go_arounds"] += 1
                    r -= w["go_around"]
            r -= w["per_aircraft_minute"] * len(s.world.pilots) / 60.0
            if s.t % 2 == 0:
                r += self._conflicts()
        return r, events

    def _gap_at_landing(self, cs: str, views: dict) -> float:
        """When an aircraft lands: how far the next one on final is from its target gap."""
        followers = sorted((v for v in views.values() if v.cs != cs and v.loc and v.along > 0), key=lambda v: v.along)
        lead = views.get(cs)
        if not followers or lead is None:
            return 0.0
        f = followers[0]
        target = max(wake.RADAR_MINIMUM_NM, wake.separation_nm(lead.actype, f.actype)) + TARGET_BUFFER_NM
        self.gaps.append((round(f.along, 2), round(target, 2)))
        err = f.along - target
        if err < 0:
            return -self.reward_w["gap_too_close_per_nm"] * -err
        return -self.reward_w["gap_too_far_per_nm"] * min(err, 4.0)

    def _conflicts(self) -> float:
        """New losses of separation (3 NM and 1,000 ft, 2.5 NM on the same final within 10 NM) and wake infringements."""
        views = self.session.views
        r = 0.0
        now = set()
        for i, a in enumerate(views):
            for b in views[i + 1:]:
                if abs(a.alt_ft - b.alt_ft) >= 900:
                    continue
                if a.loc and b.loc and a.along < 2.0 and b.along < 2.0:
                    continue  # runway rules, not radar separation
                minimum = 2.5 if (a.loc and b.loc and max(a.along, b.along) < 10.0) else 3.0
                if distance_xy(a.x, a.y, b.x, b.y) < minimum - 0.1:
                    now.add(tuple(sorted((a.cs, b.cs))))
        new = now - self.in_conflict
        self.in_conflict = now
        if new:
            self.counts["separation_losses"] += len(new)
            # Losses involving an aircraft in a holding stack or on its way to one: the planner's levels, not speeds.
            self.counts["losses_at_stack"] += sum(1 for pair in new
                                                  if any(self.controller.stage_of(cs) == "inbound" for cs in pair))
            r -= self.reward_w["separation_loss"] * len(new)
        finals = sorted((v for v in views if v.loc), key=lambda v: v.along)
        for lead, f in zip(finals, finals[1:]):
            need = wake.separation_nm(lead.actype, f.actype)
            pair = (lead.cs, f.cs)
            if need > wake.RADAR_MINIMUM_NM and f.along - lead.along < need - 0.1 and pair not in self.wake_flagged:
                self.wake_flagged.add(pair)
                self.counts["wake_infringements"] += 1
                r -= self.reward_w["wake_infringement"]
        return r

    def _observe(self) -> np.ndarray:
        obs, self.slots = observe(self.session.world.views(), self.controller, self.session.t)
        return obs

    def _shaping(self, obs: np.ndarray) -> float:
        """Small rewards along the way: gaps on target and aircraft on profile."""
        r = 0.0
        for row in obs[: len(self.slots)]:
            f = dict(zip(FEATURES, row))
            if f["has_ahead"] and abs(f["gap_ahead_nm"] - f["gap_needed_nm"]) * 10.0 <= 0.5:
                r += self.reward_w["on_target"]
            if abs(f["above_profile_ft"]) * 3000.0 <= 1000.0:
                r += self.reward_w["on_profile"]
        return r
