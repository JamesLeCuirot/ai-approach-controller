"""Stage 2's training world: the planner proposes, the AI adjusts.

The rules controller plans the sequence, releases aircraft from the hold and proposes a heading, level and speed
for every aircraft (RulesController(proposals=True)), planning height as well as time and timing everything with
the wind. Every 10 seconds, for each aircraft in the landing sequence, the AI chooses:

* heading: as planned, or 5 or 10 degrees left or right of the planned heading;
* level: as planned, or 1,000 ft above or below the planned level;
* speed: as planned, or 10 or 20 kt slower or faster, inside the published band.

"As planned" is always choice 0, so copying the planner is exact, and every alternative can be taken at every
step (in stage 1b, "not yet" could only be taken in the 10 seconds before a planned turn and was almost never
tried). The adjustment holds until the AI changes it: whenever the planner's proposal changes (a turn onto base,
a new level), the AI's adjustment is applied to the new proposal at once. A clearance is said to the pilot only
when it differs from the one the aircraft already has, so an adjustment kept over many steps is one instruction.

A heading can be adjusted until the aircraft captures the localiser, a level until it captures the glide path,
and a speed wherever the controller gives speeds (approach.controllers.speed). Pilots fly like real arrivals
(World(pilots="measured")) and the safety check's look-ahead is the model of how aircraft respond
(approach.response); every instruction still goes through it.

The AI sees stage 1a's picture of each aircraft plus what vectoring needs: what it can adjust now, how far it is
off the planned path, its heading against the planned heading, the track miles to its join point and its angle
to the final approach course, its height margin (track miles to the join point less those it needs to get just
below the glide path there), its height above that, the time since its last instruction, and the adjustments in
force.

VectorController flies the same thing in an ordinary session (replays and the benchmark), with a trained policy
or with "as planned" everywhere (the comparison: it flies exactly as the planner would).
"""

from __future__ import annotations

import math

import numpy as np

try:
    from gymnasium import spaces
except ImportError:
    spaces = None

from approach.ai.env import FEATURES, N_MAX, SpeedEnv, observe
from approach.controllers import speed
from approach.controllers.base import Controller
from approach.controllers.rules import RulesController
from approach.geo import bearing_xy, distance_xy, heading_difference
from approach.instructions import Instruction

# The stage 2 planner: the base turn planned to the landing slot, slot speeds, timed with the wind, planning its
# descents for pilots who descend at real rates.
PLANNER_2 = {"fixed_paths": False, "speeds": "slot", "wind_aware": True, "descent": "plan"}
HEADING_OFFSETS = (0, -10, -5, 5, 10)  # degrees; as planned first
LEVEL_OFFSETS = (0, -1000, 1000)  # ft
SPEED_OFFSETS = (0, -20, -10, 10, 20)  # kt
CHOICES = (len(HEADING_OFFSETS), len(LEVEL_OFFSETS), len(SPEED_OFFSETS))
FEATURES_2 = FEATURES + (
    "can_heading", "can_level", "can_speed", "off_path_nm", "heading_vs_plan", "to_join_nm", "join_angle",
    "height_margin_nm", "above_join_alt_ft", "since_instruction", "heading_offset", "level_offset", "speed_offset",
)
SAME_HEADING_DEG, SAME_LEVEL_FT, SAME_SPEED_KT = 2.5, 100.0, 5.0  # closer than this to the clearance: nothing to say


def can_adjust(p, v) -> tuple[bool, bool, bool]:
    """Whether the AI can adjust this aircraft's heading, level and speed now."""
    vectored = p is not None and p.stage in ("downwind", "base", "cleared")
    return (vectored and not v.loc, vectored and not v.gs,
            vectored and speed.band(p.stage, v.along) is not None)


class VectorController(Controller):
    """The planner's proposals, with the AI's adjustments, as instructions."""

    name = "ai_vector"

    def __init__(self, airport, runway: str, policy=None, name: str = "ai_vector", planner: dict | None = None):
        self.rules = RulesController(airport, runway, **{**PLANNER_2, **(planner or {})}, proposals=True)
        self.airport = airport
        self.policy = policy
        self.name = name
        self.offsets: dict[str, tuple[int, int, int]] = {}  # aircraft -> (heading, level, speed) choice
        self.last_t = -1e9
        self.last_said: dict[str, float] = {}  # aircraft -> when it was last given a heading, level or speed
        self.ai_said: set[tuple[str, str, float]] = set()  # instructions that differ from the plan (the AI's own)
        self.ai_instructions = 0
        self.ai_blocked = 0

    @property
    def plans(self):
        return self.rules.plans

    def stage_of(self, cs: str):
        return self.rules.stage_of(cs)

    def set_action(self, action, slots: list[str]) -> None:
        """The AI's choices for the aircraft in the sequence: [heading per place] + [level ...] + [speed ...]."""
        a = np.asarray(action, dtype=np.int64)
        self.offsets = {cs: (int(a[i]), int(a[N_MAX + i]), int(a[2 * N_MAX + i])) for i, cs in enumerate(slots)}

    def clearances(self, t: float, views: list) -> list[tuple[Instruction, bool]]:
        """The instructions that bring each aircraft to its planned clearance plus the AI's adjustment, as
        (instruction, the AI's own: it differs from the plan)."""
        out = []
        for v in views:
            p = self.rules.plans.get(v.cs)
            if p is None:
                continue
            ch, cl, cs_ = self.offsets.get(v.cs, (0, 0, 0))
            can_h, can_l, can_s = can_adjust(p, v)
            if p.hdg_cmd is not None:
                dh = HEADING_OFFSETS[ch] if can_h else 0
                want = self.rules._snap(p.hdg_cmd + dh) if dh else p.hdg_cmd
                if abs(heading_difference(want, v.target_hdg)) >= SAME_HEADING_DEG and not v.loc:
                    out.append((Instruction(v.cs, "heading", want), dh != 0))
            if p.alt_cmd:  # None, or 0 after a block: the planner will ask again
                dl = LEVEL_OFFSETS[cl] if can_l else 0
                want = p.alt_cmd + dl
                if abs(want - v.target_alt) >= SAME_LEVEL_FT and not v.gs:
                    out.append((Instruction(v.cs, "altitude", want), dl != 0))
            limits = speed.band(p.stage, v.along)
            planned = p.spd_cmd if p.spd_cmd is not None else v.target_spd
            ds = SPEED_OFFSETS[cs_] if can_s else 0
            if limits is not None and (p.spd_cmd is not None or ds):
                want = speed.in_band(planned + ds, limits)
                if abs(want - v.target_spd) >= SAME_SPEED_KT:
                    out.append((Instruction(v.cs, "speed", want), ds != 0 and want != speed.in_band(planned, limits)))
        return out

    def note_said(self, t: float, instr: Instruction, own: bool) -> None:
        self.last_said[instr.callsign] = t
        if own:
            self.ai_instructions += 1
            self.ai_said.add((instr.callsign, instr.kind, float(instr.value)))

    def decide(self, t: float, views: list) -> list[Instruction]:
        other = self.rules.decide(t, views)  # directs, holds and ILS clearances; the rest are proposals
        if self.policy is not None and t - self.last_t >= speed.DECISION_S:
            self.last_t = t
            obs, slots = observe_vector(views, self, t)
            self.set_action(self.policy(obs), slots)
        out = []
        for instr, own in self.clearances(t, views):
            self.note_said(t, instr, own)
            out.append(instr)
        return out + other

    def on_event(self, event: dict) -> None:
        if event.get("kind") == "blocked":
            instr = event["instruction"]
            if (instr.callsign, instr.kind, float(instr.value)) in self.ai_said:
                self.ai_blocked += 1
        self.rules.on_event(event)


def _off_path_nm(rules: RulesController, p, v) -> float:
    """How far the aircraft is from its planned path (the downwind, base, intercept and final)."""
    from shapely.geometry import LineString, Point

    pts = rules._points(p)
    thr = rules.airport.xy(rules.runway.threshold)
    outbound = (rules.runway.course_true + 180.0) % 360.0
    far = (pts["D"][0] + 15.0 * math.sin(math.radians(outbound)), pts["D"][1] + 15.0 * math.cos(math.radians(outbound)))
    legs = {"downwind": [far, pts["D"], pts["B"], pts["J"], thr], "base": [pts["D"], pts["B"], pts["J"], thr],
            "cleared": [pts["B"], pts["J"], thr]}[p.stage]
    return LineString(legs).distance(Point(v.x, v.y))


def observe_vector(views: list, ctl: VectorController, t: float) -> tuple[np.ndarray, list[str]]:
    """Stage 1a's picture of each aircraft in the sequence, plus what vectoring needs."""
    rules = ctl.rules
    base, slots = observe(views, rules, t)
    obs = np.zeros((N_MAX, len(FEATURES_2)), dtype=np.float32)
    obs[:, : len(FEATURES)] = base
    by_cs = {v.cs: v for v in views}
    course = rules.runway.course_true
    for i, cs in enumerate(slots):
        v, p = by_cs[cs], rules.plans[cs]
        can_h, can_l, can_s = can_adjust(p, v)
        plan_hdg = p.hdg_cmd if p.hdg_cmd is not None else v.target_hdg
        to_join = rules._track_to_join(v, p) if p.stage != "cleared" else distance_xy(v.x, v.y, *rules._points(p)["J"])
        join_alt = rules._join_alt(p.d_join)
        margin = to_join - rules._descent_nm(v, join_alt)
        ch, cl, cs_ = ctl.offsets.get(cs, (0, 0, 0))
        extra = {
            "can_heading": float(can_h), "can_level": float(can_l), "can_speed": float(can_s),
            "off_path_nm": _off_path_nm(rules, p, v) / 5.0,
            "heading_vs_plan": heading_difference(v.hdg, plan_hdg) / 45.0,
            "to_join_nm": to_join / 20.0, "join_angle": heading_difference(v.hdg, course) / 90.0,
            "height_margin_nm": margin / 10.0, "above_join_alt_ft": (v.alt_ft - join_alt) / 3000.0,
            "since_instruction": min(t - ctl.last_said.get(cs, -1e9), 120.0) / 120.0,
            "heading_offset": HEADING_OFFSETS[ch] / 10.0, "level_offset": LEVEL_OFFSETS[cl] / 1000.0,
            "speed_offset": SPEED_OFFSETS[cs_] / 20.0,
        }
        obs[i, len(FEATURES):] = [extra[f] for f in FEATURES_2[len(FEATURES):]]
    return np.clip(obs, -5.0, 5.0), slots


def as_planned() -> np.ndarray:
    """"As planned" for every aircraft: the planner's own clearances (the head start's teacher)."""
    return np.zeros(3 * N_MAX, dtype=np.int64)


class VectorEnv(SpeedEnv):
    """Stage 2: one episode = one real hour of arrivals, one step = 10 seconds of the AI adjusting the planner."""

    def __init__(self, *args, planner: dict | None = None, **kwargs):
        kwargs.setdefault("pilots", "measured")
        kwargs.setdefault("lookahead", "model")
        super().__init__(*args, planner={**PLANNER_2, **(planner or {})}, **kwargs)
        self.observation_space = spaces.Box(-5.0, 5.0, shape=(N_MAX, len(FEATURES_2)), dtype=np.float32)
        self.action_space = spaces.MultiDiscrete([CHOICES[0]] * N_MAX + [CHOICES[1]] * N_MAX + [CHOICES[2]] * N_MAX)

    def _make_controller(self, runway: str):
        self.vector = VectorController(self.airport, runway, planner=self.planner)
        self.vector.name = "rules_vector" if self.teacher else "ai_vector"
        return self.vector.rules, self.vector

    def teacher_action(self) -> np.ndarray:
        return as_planned()

    def _act(self, action) -> float:
        s, r, ctl = self.session, 0.0, self.vector
        s.views = s.world.views()
        self._downwind = set()
        before_blocked = ctl.ai_blocked
        ctl.set_action(action, self.slots)
        for instr, own in ctl.clearances(s.t, s.views):
            ok, _ = s.instruct(instr)
            ctl.note_said(s.t, instr, own)
            if own:
                self.counts["instructions"] += 1
                r -= self.reward_w["instruction"]
            if not ok:
                self.counts["blocked"] += 1
        # The AI's own instructions blocked here or since the last step (the planner's are its own business).
        r -= self.reward_w["blocked"] * (ctl.ai_blocked - before_blocked)
        return r

    def _observe(self) -> np.ndarray:
        obs, self.slots = observe_vector(self.session.world.views(), self.vector, self.session.t)
        return obs
