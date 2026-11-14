"""Stage 2's look-ahead controller: it tries changes in copies of the simulator before giving them.

Every LOOK_EVERY_S it flies the next minutes forward from the current moment in copies of the simulator (forked
processes), keeping every adjustment in force (with nothing in force that is the planner's own clearances), DRAWS
times. In each copy every pilot is drawn again from the measured spread (approach.sim.world, pilots="measured"),
and so are the pilots' response delays: a controller does not know how a particular crew will fly. If any copy
shows a problem, it flies the same copies again with each single move it could make to the aircraft involved, and
gives the move whose copies score best, if they score at least MIN_GAIN better than doing nothing. A move in force
is also tried taken back at every look, and taken back when that scores no worse. One move at a time: at most one
per look.

The score is the training world's reward over the copies' minutes (5 for each loss of separation, 3 for each
go-around or wake infringement, 1 for each landing, and the small terms for gaps, profile, instructions and time
in the sector), less gap_weight for each gap inside the minimum at a landing.

Two sets of moves (``moves``):

* "small" (the first version, runs/lookahead/v1_120): looks HORIZON_S (5 minutes) ahead; the problems are losses
  of separation and go-arounds; the moves are stage 2's AI's (approach.ai.vector, hold=True) for the aircraft
  involved (and the aircraft ahead of one that goes around): a heading 5 or 10 degrees either way, a level 1,000 ft
  up or down, a speed 10 or 20 kt either way, held until taken back.
* "wide": looks HORIZON_WIDE_S (7 minutes) ahead, so a decision on the downwind can see the landing; a gap inside
  the minimum is a problem too (its follower and leader are involved), counting GAP_WEIGHT; and two more moves:
  keep an aircraft in or on its way to a stack where it is, or 1,000 ft above or below that, until taken back (a
  holding aircraft cleared down through, or into, a level another holding aircraft will be at was the commonest
  loss of separation left; these aircraft aren't yet in the landing sequence the AI's other moves act on); and
  extend an aircraft's downwind by 1 or 2 NM before its base turn (a gap inside the minimum on final can't be
  opened once the follower is on the localiser). A move stays in force at least MIN_KEEP_S before it can be
  taken back: three draws give a noisy picture, and without it a stack level was given, taken back and given
  again a minute apart.

What the copies know is what a controller would: the traffic in the sector and the arrivals due in the next few
minutes (the scenario's real arrivals spawn in the copies at their real times, as their flight strips would show),
the wind, the planner's plan, and the safety check. They do not know how each crew will fly.

The look-ahead flies in LookAheadEnv: stage 2's training world (VectorEnv, hold=True) with a controller that also
takes the wide moves. With no move made it flies exactly as VectorEnv does, so its sessions compare like for like
with the competitor's ("as planned" everywhere).

    PYTHONPATH=. python -m approach.ai.lookahead --moves wide --sessions 120 --workers 4 --out runs/lookahead/v2_120
"""

from __future__ import annotations

import argparse
import json
import math
import os
import random
import time
from collections import Counter

import numpy as np

from approach.ai.vector import FEATURES_2, N_MAX, OFFSETS, PLANNED, VectorController, VectorEnv
from approach.instructions import Instruction

LOOK_EVERY_S = 30
HORIZON_S = 300
HORIZON_WIDE_S = 420
DRAWS = 3
MIN_GAIN = 1.0  # a move must score this much better than doing nothing (a fifth of a loss of separation)
GAP_WEIGHT = 3.0  # "wide": each gap inside the minimum at a landing, in the score
EXTEND_NM = (1.0, 2.0)
MIN_KEEP_S = 120  # "wide": a move stays in force at least this long before it can be taken back
CAN = [FEATURES_2.index(f) for f in ("can_heading", "can_level", "can_speed")]
DIMS = ("heading", "level", "speed")
REVERTS = ("release", "set_planned")


def stack_hold_level(v, step: int = 0) -> float | None:
    """The level LookAheadController.hold_level keeps an aircraft at, or None where it can't (below the stack)."""
    from approach.controllers.rules import STACK_BASE_FT

    level = v.target_alt
    if v.target_alt < v.alt_ft - 100:
        level = max(v.target_alt, math.ceil((v.alt_ft - 300) / 1000.0) * 1000.0)
    level += 1000.0 * step
    return level if level >= STACK_BASE_FT else None


class LookAheadController(VectorController):
    """Stage 2's controller (hold=True), which can also stop an aircraft in or on its way to a stack at a level,
    and extend an aircraft's downwind."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **{**kwargs, "hold": True})
        self.held_level: dict[str, float] = {}
        self.touched: set[str] = set()  # aircraft given a stack level, until they leave the stack

    def decide(self, t: float, views: list) -> list[Instruction]:
        self._stack_order(views)
        return super().decide(t, views)

    def _stack_order(self, views: list) -> None:
        """In a stack where an aircraft was given a level, the aircraft holding leave lowest first.

        The planner's queue follows the order the aircraft joined the stack, and its lowest aircraft leaves first,
        so the two agree only while the stack fills from the bottom in that order. An aircraft kept high can join
        above aircraft that joined after it, and then neither can leave: the higher one waits for the one below,
        which waits behind it in the queue. In such a stack the queue goes by height instead.
        """
        plans = self.rules.plans
        self.touched = {cs for cs in self.touched if cs in plans and plans[cs].stage == "inbound"}
        by_cs = {v.cs: v for v in views}
        for fix in {plans[cs].hold_fix for cs in self.touched}:
            members = [p for p in plans.values() if p.stage == "inbound" and p.holding and not p.below_stack
                       and p.hold_fix == fix and p.cs in by_cs]
            keys = sorted(p.key for p in members)
            by_height = sorted(members, key=lambda p: by_cs[p.cs].alt_ft)
            if [p.key for p in by_height] != keys:
                for p, key in zip(by_height, keys):
                    p.key = key
                self.rules.last_plan_t = -1e9  # replan now

    def hold_level(self, cs: str, views: list, step: int = 0) -> bool:
        """Keep an aircraft in or on its way to a stack where it is (step 0): at its cleared level, or, if
        descending, at the level above it; or 1,000 ft above (step 1) or below (step -1) that, never below the
        lowest stack level."""
        v = next((w for w in views if w.cs == cs), None)
        p = self.rules.plans.get(cs)
        if v is None or p is None or p.stage != "inbound":
            return False
        level = stack_hold_level(v, step)
        if level is None:
            return False
        self.held_level[cs] = level
        self.touched.add(cs)
        return True

    def release(self, cs: str) -> None:
        """Back to the planner's levels. Its last level may be long out of date (it was held back), so it chooses
        a free level again rather than have the old one given now."""
        if self.held_level.pop(cs, None) is not None:
            p = self.rules.plans.get(cs)
            if p is not None:
                p.alt_cmd = None

    def release_now(self, cs: str, views: list) -> bool:
        """Off its stack now, onto its downwind, though the planner would keep it holding longer: only the lowest in
        its stack, inside the RMA, as the planner itself releases (RulesController._replan), which then plans its
        path and descent."""
        rules = self.rules
        by_cs = {v.cs: v for v in views}
        p, v = rules.plans.get(cs), by_cs.get(cs)
        if p is None or v is None or p.stage != "inbound" or not p.holding:
            return False
        if (not p.below_stack and rules._anyone_below_in_stack(p, by_cs)) or not rules._inside(v.x, v.y):
            return False
        side = rules._side(v)
        if rules.max_join(side) < 0:
            return False
        p.stage, p.side, p.holding = "downwind", side, False
        ahead = sum(1 for q in rules.plans.values() if q.stage == "downwind" and q.cs in by_cs)
        p.level = min(6000, 4000 + 1000 * ahead)
        p.alt_cmd = None
        p.hdg_cmd = None
        self.held_level.pop(cs, None)
        rules.last_plan_t = -1e9
        return True

    def extend_downwind(self, cs: str, nm: float) -> bool:
        """A longer downwind (a later base turn) for an aircraft not yet turned onto base."""
        p = self.rules.plans.get(cs)
        if p is None or p.stage != "downwind" or p.d_join + nm > self.rules.max_join(p.side) + 1e-6:
            return False
        p.d_join += nm  # the planner never shortens a downwind, so this holds
        self.rules.last_plan_t = -1e9
        return True

    def clearances(self, t: float, views: list) -> list[tuple[Instruction, bool]]:
        out = super().clearances(t, views)
        if not self.held_level:
            return out
        by_cs = {v.cs: v for v in views}
        for cs in list(self.held_level):
            p = self.rules.plans.get(cs)
            if p is None or p.stage != "inbound" or cs not in by_cs:
                del self.held_level[cs]  # off the stack (or gone): the planner's again
        out = [(i, own) for i, own in out if not (i.kind == "altitude" and i.callsign in self.held_level)]
        for cs, level in self.held_level.items():
            if abs(by_cs[cs].target_alt - level) >= 100:
                out.append((Instruction(cs, "altitude", level), True))
        return out

    def on_event(self, event: dict) -> None:
        if event.get("kind") == "blocked":
            instr = event["instruction"]
            if instr.kind == "altitude" and instr.callsign in self.held_level:
                self.held_level.pop(instr.callsign, None)
        super().on_event(event)


class LookAheadEnv(VectorEnv):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **{**kwargs, "hold": True})

    def _make_controller(self, runway: str):
        self.vector = LookAheadController(self.airport, runway, planner=self.planner)
        self.vector.name = "rules_vector" if self.teacher else "ai_vector"
        return self.vector.rules, self.vector


def apply_move(env: VectorEnv, move: tuple | None) -> np.ndarray:
    """Make a move: the choices for the step, with any other move made on the controller."""
    action = np.zeros(3 * N_MAX, dtype=np.int64)  # keep everywhere
    if move is None:
        return action
    kind, cs = move[0], move[1]
    ctl = env.vector
    if kind in ("set", "set_planned") and cs in env.slots:
        action[move[2] * N_MAX + env.slots.index(cs)] = move[3]
    elif kind == "hold_level":
        ctl.hold_level(cs, env.session.world.views(), move[2] if len(move) > 2 else 0)
    elif kind == "release":
        ctl.release(cs)
    elif kind == "extend":
        ctl.extend_downwind(cs, move[2])
    elif kind == "release_now":
        ctl.release_now(cs, env.session.world.views())
    return action


def redraw_pilots(world, seed: int) -> None:
    """Draw every pilot again from the measured spread, and new response delays: how a copy's crews fly."""
    from approach.sim.world import (_BOUNDS, FINAL_SLOWING_BOUNDS, FPM_TO_MS, draw, final_slowing_quantiles,
                                    response_quantiles)

    world.rng = random.Random(seed)
    world.pilot_rng = random.Random(seed * 7919 + 17)
    if world.pilot_model != "measured":
        return
    q = response_quantiles(world.airport.icao)
    table = final_slowing_quantiles(world.airport.icao)
    for cs, p in world.pilots.items():
        for name in _BOUNDS:
            setattr(p, name, draw(q[name], world.pilot_rng.random(), _BOUNDS[name]))
        if table:
            nearest = min(table, key=lambda k: abs(k - p.approach_speed))
            p.final_slowing_kt_s = draw(table[nearest], world.pilot_rng.random(), FINAL_SLOWING_BOUNDS)
        i = world._idx(cs)
        if i >= 0:
            world.bs.traf.ap.bankdef[i] = math.radians(p.bank_deg)
            world.bs.traf.ap.vsdef[i] = p.descent_fpm * FPM_TO_MS


def _instrument(env: VectorEnv, log: dict) -> None:
    """Record each new loss of separation (the pair), go-around and gap inside the minimum, in this copy."""
    conflicts, advance = type(env)._conflicts.__get__(env), type(env)._advance.__get__(env)
    gap_at_landing = type(env)._gap_at_landing.__get__(env)

    def _conflicts():
        old = set(env.in_conflict)
        r = conflicts()
        log["losses"] += [list(pair) for pair in sorted(env.in_conflict - old)]
        return r

    def _advance(seconds):
        r, events = advance(seconds)
        log["go_arounds"] += [e["cs"] for e in events if e["kind"] == "go_around"]
        return r, events

    def _gap(cs, views):
        n = len(env.gaps)
        r = gap_at_landing(cs, views)
        if len(env.gaps) > n and env.gaps[-1][0] < env.gaps[-1][1] - 0.5:
            follower = min((v for v in views.values() if v.cs != cs and v.loc and v.along > 0), key=lambda v: v.along)
            log["gaps"].append([cs, follower.cs])
        return r

    env._conflicts, env._advance, env._gap_at_landing = _conflicts, _advance, _gap


class _FastLinear:
    """BlueSky's wind interpolation (scipy's RegularGridInterpolator, linear, 0 outside the grid) without scipy's
    checks on every call: the same arithmetic, the eight corners of each grid cell at once, and the last answer
    reused when asked again for the same positions (BlueSky asks several times a step). It was 30% of a copy."""

    def __init__(self, rgi):
        axes, values = [np.asarray(g, dtype=float) for g in rgi.grid], np.asarray(rgi.values, dtype=float)
        if len(axes) != 3 or any(len(g) < 2 for g in axes):
            raise ValueError("a 3-D grid with two points on each axis")
        for d, g in enumerate(axes):
            if g[0] > g[-1]:
                axes[d], values = g[::-1], np.flip(values, axis=d)
        self.axes, self.fill, self.grid = axes, rgi.fill_value, rgi.grid
        self.flat = np.ascontiguousarray(values).ravel()
        strides = np.array([values.shape[1] * values.shape[2], values.shape[2], 1])
        self.strides = strides
        self.offsets = np.array([c0 * strides[0] + c1 * strides[1] + c2 * strides[2]
                                 for c0 in (0, 1) for c1 in (0, 1) for c2 in (0, 1)])
        self.lo = np.array([g[0] for g in axes])
        self.hi = np.array([g[-1] for g in axes])
        self._last_in, self._last_out = None, None

    def __call__(self, pts):
        pts = np.asarray(pts, dtype=float).reshape(-1, 3)
        if self._last_in is not None and pts.shape == self._last_in.shape and np.array_equal(pts, self._last_in):
            return self._last_out.copy()
        n = len(pts)
        i = np.empty((n, 3), dtype=np.int64)
        w = np.empty((n, 3))
        for d, g in enumerate(self.axes):
            k = np.clip(np.searchsorted(g, pts[:, d], side="right") - 1, 0, len(g) - 2)
            i[:, d] = k
            w[:, d] = (pts[:, d] - g[k]) / (g[k + 1] - g[k])
        base = i @ self.strides
        vals = self.flat[base[:, None] + self.offsets[None, :]]  # (n, 8), corners in (alt, lat, lon) bit order
        w0 = np.stack([1.0 - w[:, 0], w[:, 0]], axis=1)
        w1 = np.stack([1.0 - w[:, 1], w[:, 1]], axis=1)
        w2 = np.stack([1.0 - w[:, 2], w[:, 2]], axis=1)
        weights = (w0[:, :, None, None] * w1[:, None, :, None] * w2[:, None, None, :]).reshape(n, 8)
        out = (weights * vals).sum(axis=1)
        if self.fill is not None:
            out[((pts < self.lo) | (pts > self.hi)).any(axis=1)] = self.fill
        self._last_in, self._last_out = pts.copy(), out
        return out.copy()


COPY_DT_S = 1.0  # the simulator's step in the copies (the session itself steps every 0.5 s)


def quick_copy(env: VectorEnv, dt_s: float | None = COPY_DT_S) -> None:
    """In a forked child: fly the copy with the faster wind interpolation and the simulator stepping every dt_s.
    The copies are previews, so a step of a second (pilots respond in several) costs them little; together these
    roughly halve a copy's time. The session itself is never changed."""
    world = env.session.world
    bs = world.bs
    wind = bs.traf.wind
    if wind.fe is not None and not isinstance(wind.fe, _FastLinear):
        wind.fe, wind.fn = _FastLinear(wind.fe), _FastLinear(wind.fn)
    if dt_s and abs(dt_s - bs.sim.simdt) > 1e-9:
        from bluesky.core import simtime

        simtime.setdt(dt_s)
        bs.sim.simdt = dt_s
        world.steps_per_second = max(1, round(1.0 / dt_s))


# Checked on 135 looks of three busy training sessions: given the same pilots, quick copies give the full copies'
# verdict (trouble or not) 97% of the time and name 92% of the same aircraft, where two sets of full copies with
# different pilots agree 90% and 66%; they took 208 s to the full copies' 328 s.
QUICK_COPIES = True


def _copy(env: VectorEnv, seed: int, move: tuple | None, horizon_s: float) -> dict:
    """In a forked child: make the move, then fly on with fresh pilots, keeping everything, for horizon_s."""
    if QUICK_COPIES:
        quick_copy(env)
    redraw_pilots(env.session.world, seed)
    log = {"losses": [], "go_arounds": [], "gaps": []}
    _instrument(env, log)
    action = apply_move(env, move)
    keep = np.zeros_like(action)
    t_end, total, done, first, minutes = env.session.t + horizon_s, 0.0, False, True, 0.0
    while not done and env.session.t < t_end:
        t0 = env.session.t
        _, r, done, _, _ = env.step(action if first else keep)
        first = False
        total += float(r)
        minutes += len(env.session.world.views()) * (env.session.t - t0) / 60.0
    # The minutes the planner still expects each aircraft to need at the end (its landing time; for one holding,
    # the slot it would be given): what a move does to the queue beyond the copy's minutes.
    views = {v.cs: v for v in env.session.world.views()}
    remaining = sum(max(p.landing_t - env.session.t, 0.0) for p in env.vector.rules.plans.values()
                    if p.cs in views) / 60.0
    return {"reward": total, "aircraft_minutes": minutes, "remaining_minutes": remaining, **log}


def fly_copies(env: VectorEnv, jobs: list[tuple[int, tuple | None]], workers: int = 1,
               horizon_s: float = HORIZON_S) -> list[dict]:
    """Fly each (pilot seed, move) from the current moment in its own forked copy of the simulator."""
    out, running, queue = [None] * len(jobs), {}, list(enumerate(jobs))
    while queue or running:
        while queue and len(running) < workers:
            i, (seed, move) = queue.pop(0)
            r, w = os.pipe()
            pid = os.fork()
            if pid == 0:  # the copy
                os.close(r)
                try:
                    res = _copy(env, seed, move, horizon_s)
                except Exception as e:  # noqa: BLE001
                    res = {"error": repr(e)}
                with os.fdopen(w, "w") as f:
                    json.dump(res, f)
                os._exit(0)
            os.close(w)
            running[pid] = (i, r)
        pid, _ = os.wait()
        i, r = running.pop(pid)
        with os.fdopen(r) as f:
            text = f.read()
        out[i] = json.loads(text) if text else {"error": "no result"}
    return out


class LookAhead:
    """Chooses stage 2's adjustments (and, with moves="wide", the wider moves) by trying them in copies."""

    def __init__(self, workers: int = 1, draws: int = DRAWS, min_gain: float = MIN_GAIN, seed: int = 0,
                 moves: str = "small", time_weight: float = 0.0, releases: bool = False):
        """``time_weight`` (efficient look-ahead): each aircraft-minute in the sector during the copies, and each
        minute the planner still expects aircraft to need at their end, costs this much in the score (a loss of
        separation costs 5). ``releases``: also try taking the lowest aircraft of each stack off it now."""
        if moves not in ("small", "wide"):
            raise ValueError("moves must be small or wide")
        self.workers, self.draws, self.min_gain, self.moves = workers, draws, min_gain, moves
        self.time_weight, self.releases = time_weight, releases
        self.horizon_s = HORIZON_WIDE_S if moves == "wide" else HORIZON_S
        self.gap_weight = GAP_WEIGHT if moves == "wide" else 0.0
        self.rng = random.Random(seed)
        self.last_look = -1e9
        self.made: dict[tuple, float] = {}  # when each move in force was made: (cs, dimension or "stack") -> t
        self.stats = Counter()
        self.changes: list[dict] = []

    def reset(self) -> None:
        self.last_look = -1e9
        self.made = {}

    def score(self, copy: dict) -> float:
        time = copy.get("aircraft_minutes", 0.0) + copy.get("remaining_minutes", 0.0)
        return copy["reward"] - self.gap_weight * len(copy.get("gaps", [])) - self.time_weight * time

    def candidates(self, env: VectorEnv, obs: np.ndarray, involved: set[str]) -> list[tuple]:
        """Taking back each move in force, and every move for each aircraft involved, where it can be made now."""
        ctl, out = env.vector, []
        rules = ctl.rules
        views = {v.cs: v for v in env.session.world.views()}
        for i, cs in enumerate(env.slots):
            now = ctl.offsets.get(cs, (0, 0, 0))
            for b in range(3):
                if obs[i, CAN[b]] < 0.5:
                    continue
                if now[b] != 0 and self.can_take_back(env, (cs, b)):
                    out.append(("set_planned", cs, b, PLANNED))
                if cs in involved:
                    out += [("set", cs, b, PLANNED + k) for k in range(1, len(OFFSETS[b])) if k != now[b]]
            if self.moves != "wide":
                continue
            p = rules.plans.get(cs)
            if cs in involved and p is not None and p.stage == "downwind":
                out += [("extend", cs, nm) for nm in EXTEND_NM if p.d_join + nm <= rules.max_join(p.side) + 1e-6]
        if self.moves == "wide":  # the stacks: aircraft not yet in the landing sequence (env.slots)
            from approach.controllers.rules import STACK_BASE_FT

            out += [("release", cs) for cs in sorted(ctl.held_level) if self.can_take_back(env, (cs, "stack"))]
            if self.releases:  # the lowest holding aircraft of each stack, off it now (the planner would wait)
                from approach.controllers.rules import STACKS

                for fix in STACKS:
                    members = [p for p in rules._stack(fix, views) if p.holding and p.cs in views]
                    if members:
                        low = min(members, key=lambda p: views[p.cs].alt_ft)
                        if not rules._anyone_below_in_stack(low, views):
                            out.append(("release_now", low.cs))
            for cs in sorted(involved):
                v = views.get(cs)
                if v is None or rules.stage_of(cs) != "inbound":
                    continue
                steps = (1, -1) if cs in ctl.held_level else (0, 1, -1)
                out += [("hold_level", cs, k) for k in steps if k >= 0 or v.target_alt - 1000 >= STACK_BASE_FT]
        return out

    def can_take_back(self, env: VectorEnv, key: tuple) -> bool:
        """"wide": a move stays in force at least MIN_KEEP_S (a controller doesn't take back an instruction
        moments after giving it)."""
        return self.moves != "wide" or env.session.t - self.made.get(key, -1e9) >= MIN_KEEP_S

    def choose(self, env: VectorEnv, obs: np.ndarray) -> np.ndarray:
        """The choices for this step: keep everything, except at a look, when one move may be made."""
        t = env.session.t
        if t - self.last_look < LOOK_EVERY_S:
            return apply_move(env, None)
        self.last_look = t
        anything = (obs[:len(env.slots)][:, CAN] > 0.5).any()
        if self.moves == "wide":  # an aircraft in or on its way to a stack can be stopped where it is
            rules = env.vector.rules
            anything = anything or any(rules.stage_of(v.cs) == "inbound" for v in env.session.world.views())
        if not anything:  # nothing it could change: no need to look
            self.stats["nothing_to_adjust"] += 1
            return apply_move(env, None)
        t0 = time.time()
        try:
            move = self._look(env, obs, t)
        finally:
            self.stats["look_seconds"] += time.time() - t0
        return apply_move(env, move)

    def probe(self, env: VectorEnv) -> tuple[list[int], list[dict], set[str]]:
        """Fly the copies with nothing changed: their pilot seeds, results, and the aircraft that ran into trouble
        in any of them. Also keeps the share of the copies each aircraft ran into trouble in (last_involved)."""
        self.last_involved = {}
        seeds = [self.rng.randrange(1 << 30) for _ in range(self.draws)]
        base = fly_copies(env, [(s, None) for s in seeds], self.workers, self.horizon_s)
        base = [b for b in base if "error" not in b]
        self.stats["looks"] += 1
        self.stats["copies"] += len(seeds)
        involved, in_draws = set(), Counter()
        for b in base:
            mine = set()
            for pair in b["losses"]:
                mine.update(pair)
            for cs in b["go_arounds"]:
                mine.add(cs)
                if cs in env.slots and env.slots.index(cs) > 0:
                    mine.add(env.slots[env.slots.index(cs) - 1])  # the aircraft ahead of it
            if self.moves == "wide":
                for pair in b["gaps"]:
                    mine.update(pair)
            involved |= mine
            in_draws.update(mine)
        # The share of the copies in which each aircraft ran into trouble (approach.ai.imitate records it).
        self.last_involved = {cs: n / len(base) for cs, n in in_draws.items()} if base else {}
        return seeds, base, involved

    def _look(self, env: VectorEnv, obs: np.ndarray, t: float) -> tuple | None:
        seeds, base, involved = self.probe(env)
        if not base:
            return None
        if involved:
            self.stats["looks_with_a_problem"] += 1
        cands = self.candidates(env, obs, involved)
        if not cands:
            return None
        jobs = [(s, cand) for cand in cands for s in seeds]
        res = fly_copies(env, jobs, self.workers, self.horizon_s)
        self.stats["copies"] += len(jobs)
        base_score = float(np.mean([self.score(b) for b in base]))
        best, best_gain = None, None
        for k, cand in enumerate(cands):
            rs = [r for r in res[k * len(seeds):(k + 1) * len(seeds)] if "error" not in r]
            if len(rs) < len(seeds):
                continue
            gain = float(np.mean([self.score(r) for r in rs])) - base_score
            need = 0.0 if cand[0] in REVERTS else self.min_gain
            if gain >= need and (best_gain is None or gain > best_gain):
                best, best_gain = cand, gain
        if best is None:
            return None
        self.stats[best[0]] += 1
        self.stats["back_to_plan" if best[0] in REVERTS else "changes"] += 1
        entry = {"t": round(t), "move": best[0], "aircraft": best[1], "gain": round(best_gain, 2),
                 "problem": bool(involved)}
        if best[0] in ("set", "set_planned"):
            entry.update({"kind": DIMS[best[2]], "offset": 0 if best[3] == PLANNED else OFFSETS[best[2]][best[3] - 1]})
            self.made[(best[1], best[2])] = t
        elif best[0] == "extend":
            entry["nm"] = best[2]
        elif best[0] == "release_now":
            self.made[(best[1], "stack")] = t
        elif best[0] == "hold_level":
            entry["step_ft"] = 1000 * best[2]
            self.made[(best[1], "stack")] = t
        self.changes.append(entry)
        return best


def fly_session(env: VectorEnv, options: dict, la: LookAhead | None) -> tuple[dict, float]:
    """One test session, flown by the look-ahead controller (or, with la=None, as planned everywhere)."""
    from approach.ai.train import episode_row

    obs, _ = env.reset(options=options)
    if la is not None:
        la.reset()
    done, total, t0 = False, 0.0, time.time()
    while not done:
        act = la.choose(env, obs) if la is not None else np.zeros(3 * N_MAX, dtype=np.int64)
        obs, r, done, _, info = env.step(act)
        total += r
    return episode_row(info, total), time.time() - t0


MOVE_KINDS = ("set", "set_planned", "hold_level", "release", "extend", "release_now")


def _run(args: tuple) -> list[dict]:
    sessions, draws, min_gain, with_competitor, moves, copy, parts, time_weight, releases = args
    env = LookAheadEnv(hours=[s["hour"] for s in sessions])
    policy = None
    if copy:  # the network that copies the look-ahead (approach.ai.imitate) flies instead
        from approach.ai.imitate import CopyDriver, load_copy

        policy = load_copy(copy)
    rows = []
    for s in sessions:
        part = parts / f"session_{s['id']:03d}.json"
        if part.is_file():  # flown before the run was stopped
            rows += json.loads(part.read_text())
            continue
        options = {"hour": s["hour"], "offset_s": s["offset_s"], "pilot_seed": s["pilot_seed"]}
        meta = {"session": s["id"], "hour": f"{s['hour'][0]}T{s['hour'][1]:02d}", "offset_min": round(s["offset_s"] / 60)}
        n_before = len(rows)
        try:
            if with_competitor:
                row, secs = fly_session(env, {**options, "teacher": True}, None)
                rows.append({**meta, "controller": "rules_vector", **row})
            if policy is not None:
                la = CopyDriver(policy, probe_seed=s["pilot_seed"])
            else:
                la = LookAhead(workers=1, draws=draws, min_gain=min_gain, seed=s["pilot_seed"], moves=moves,
                               time_weight=time_weight, releases=releases)
            row, secs = fly_session(env, options, la)
            rows.append({**meta, "controller": "ai", **row, "wall_s": round(secs),
                         "looks": la.stats["looks"], "looks_with_a_problem": la.stats["looks_with_a_problem"],
                         "copies": la.stats["copies"], "changes": la.stats["changes"],
                         "back_to_plan": la.stats["back_to_plan"],
                         **{f"moves_{k}": la.stats[k] for k in MOVE_KINDS},
                         "look_s": round(la.stats["look_seconds"], 1), "change_log": json.dumps(la.changes)})
            print(f"session {s['id']}: {row['separation_losses']} losses of separation, {row['go_arounds']} go-arounds, "
                  f"{row['gaps_inside_minimum']} gaps inside the minimum, {la.stats['changes']} changes, "
                  f"{la.stats['copies']} copies, {secs / 60:.1f} min", flush=True)
            part.write_text(json.dumps(rows[n_before:]))
        except Exception as e:  # noqa: BLE001
            print(f"session {s['id']} {s['hour']} skipped: {e!r}", flush=True)
    return rows


def main() -> None:
    import csv
    from multiprocessing import Pool
    from pathlib import Path

    from approach.ai.evaluate import COMPETITOR_DIR, _fields, code_hash, real_rows, summarise, test_hours

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sessions", type=int, default=120, help="the first N of the test's 120 sessions")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--seed", type=int, default=2026)
    ap.add_argument("--draws", type=int, default=DRAWS)
    ap.add_argument("--min-gain", type=float, default=MIN_GAIN)
    ap.add_argument("--moves", default="small", choices=("small", "wide"))
    ap.add_argument("--train", action="store_true",
                    help="training hours instead of the test's (for trying settings without looking at the test)")
    ap.add_argument("--time-weight", type=float, default=0.0,
                    help="each aircraft-minute (in the copies, and still expected at their end) costs this much")
    ap.add_argument("--releases", action="store_true",
                    help="also try taking the lowest aircraft of each stack off it now (the planner would wait)")
    ap.add_argument("--copy", default="",
                    help="fly the network that copies the look-ahead (a saved approach.ai.imitate model) instead")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    if args.copy:
        args.moves = "wide"
    rng = random.Random(args.seed)
    if args.train:
        from approach.ai.env import training_hours

        hours = training_hours()
    else:
        hours = test_hours()
    sessions = [{"id": k, "hour": rng.choice(hours), "offset_s": rng.uniform(0, 3600), "pilot_seed": rng.randrange(1 << 30)}
                for k in range(120)][:args.sessions]  # the test's own sessions, in its order
    cache = COMPETITOR_DIR / f"2_seed{args.seed}_n120_{code_hash()}.csv"
    saved = list(csv.DictReader(cache.open())) if cache.is_file() and not args.train else []
    print(f"{'Reusing' if saved else 'Flying'} the competitor's results{' from ' + cache.name if saved else ''}", flush=True)
    # Each session's rows are saved as it finishes, so a run stopped part way (the machine restarted) carries on
    # from where it was when started again with the same arguments.
    parts = Path(f"{args.out}.parts")
    parts.mkdir(parents=True, exist_ok=True)
    # One session a task, handed to whichever process is free (dealt out in a fixed order, one process could be left
    # with several busy hours while the others sat idle).
    tasks = [([s], args.draws, args.min_gain, not saved, args.moves, args.copy, parts, args.time_weight, args.releases)
             for s in sessions]
    with Pool(args.workers) as pool:
        rows = [r for part in pool.map(_run, tasks, chunksize=1) for r in part]
    flown = {int(r["session"]) for r in rows if r["controller"] == "ai"}
    if saved:
        rows += [r for r in saved if int(r["session"]) in flown]
    elif not args.train:
        rows += real_rows([s for s in sessions if s["id"] in flown])
    rows.sort(key=lambda r: (int(r["session"]), r["controller"]))
    base = Path(args.out)
    base.parent.mkdir(parents=True, exist_ok=True)
    with Path(f"{base}.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=_fields(rows))
        w.writeheader()
        w.writerows(rows)
    ai = [r for r in rows if r["controller"] == "ai"]
    looks = max(sum(int(r["looks"]) for r in ai), 1)
    la = LookAhead(moves=args.moves)
    summary = {"controller": "copy" if args.copy else "lookahead", **({"copy": args.copy} if args.copy else {}),
               "moves": args.moves, "hours": "train" if args.train else "test",
               "sessions": len(flown), "seed": args.seed, "draws": args.draws, "min_gain": args.min_gain,
               "look_every_s": LOOK_EVERY_S, "horizon_s": la.horizon_s, "gap_weight": la.gap_weight,
               "time_weight": args.time_weight, "releases": args.releases, "quick_copies": QUICK_COPIES,
               "changes": sum(int(r["changes"]) for r in ai), "back_to_plan": sum(int(r["back_to_plan"]) for r in ai),
               **{f"moves_{k}": sum(int(r.get(f"moves_{k}") or 0) for r in ai) for k in MOVE_KINDS},
               "copies": sum(int(r["copies"]) for r in ai), "looks": looks,
               "looks_with_a_problem": sum(int(r["looks_with_a_problem"]) for r in ai),
               "seconds_per_look": round(sum(float(r["look_s"]) for r in ai) / looks, 1),
               "minutes_per_session": round(sum(int(r["wall_s"]) for r in ai) / 60 / max(len(ai), 1), 1),
               **summarise(rows)}
    Path(f"{base}.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
