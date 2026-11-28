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
from approach.geo import distance_xy
from approach.instructions import Instruction
from approach.sim.world import GO_AROUNDS

LOOK_EVERY_S = 30
HORIZON_S = 300
HORIZON_WIDE_S = 420
DRAWS = 3
MIN_GAIN = 1.0  # a move must score this much better than doing nothing (a fifth of a loss of separation)
GAP_WEIGHT = 3.0  # "wide": each gap inside the minimum at a landing, in the score
EXTEND_NM = (1.0, 2.0)
# An efficiency move (a release from a stack) is judged over this longer horizon: an aircraft released from a stack
# reaches final 10 to 15 minutes later, and over HORIZON_WIDE_S its copies showed no trouble from the releases that
# later brought go-arounds (the safety veto never fired on 3 dev training sessions it was tried on).
RELEASE_HORIZON_S = 900
MIN_KEEP_S = 120  # "wide": a move stays in force at least this long before it can be taken back
# stack_moves: an aircraft taken off a stack above the lowest must be at least this far from every aircraft below it
# in that stack, and releases are judged over a longer horizon, long enough to see the released aircraft land
RELEASE_CLEAR_NM = 4.0
STACK_MOVES_HORIZON_S = 1200
# stack_moves: a new stack move tried and not made is not tried again for the same aircraft for this long (each is
# judged in copies over STACK_MOVES_HORIZON_S, and trying each at every look made a busy hour take over 2 hours).
# Ordinary releases (release_now) are judged as without the stack moves: over RELEASE_HORIZON_S, at every look. With
# them judged like the new moves too, on 19 fresh training sessions the look-ahead made 14 ordinary releases instead
# of 60 and was 0.55 minutes per arrival slower, slower in 14 sessions.
RELEASE_RETRY_S = 120
NEW_STACK_MOVES = ("release_above", "straight_in")
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

    def releasable(self, cs: str, views: list, above: bool = False, inbound: bool = False) -> bool:
        """Whether release_now can take this aircraft off now (see there)."""
        return self._release_check(cs, {v.cs: v for v in views}, above, inbound) is not None

    def _release_check(self, cs: str, by_cs: dict, above: bool, inbound: bool) -> float | None:
        rules = self.rules
        p, v = rules.plans.get(cs), by_cs.get(cs)
        if p is None or v is None or p.stage != "inbound" or p.holding == inbound:
            return None
        below = not p.below_stack and rules._anyone_below_in_stack(p, by_cs)
        if below and not (above or inbound) or (above and not below) or not rules._inside(v.x, v.y):
            return None
        if above or inbound:  # only well clear of every aircraft holding below it in its stack
            for q in rules._stack(p.hold_fix, by_cs):
                w = by_cs[q.cs]
                if q is not p and q.holding and w.alt_ft < v.alt_ft - 300 and distance_xy(v.x, v.y, w.x, w.y) < RELEASE_CLEAR_NM:
                    return None
        side = rules._side(v)
        longest = rules.max_join(side)
        if longest < 0:
            return None
        if rules.descent == "plan":  # the planner's own height check: it must be able to get down on some path
            from approach.controllers.rules import Plan

            probe = Plan(**{**vars(p), "side": side, "stage": "downwind"})
            if not rules._height_ok(v, probe, longest, measured=above or inbound):
                return None
        return side

    def release_now(self, cs: str, views: list, above: bool = False, inbound: bool = False) -> bool:
        """Off its stack now, onto its downwind, though the planner would keep it holding longer: only the lowest in
        its stack, inside the RMA, and low enough to get down in time on some path, as the planner itself releases
        (RulesController._replan), which then plans its path and descent. Without the height check, 3 of the 5
        go-arounds on two busy dev training sessions were aircraft released early 9 to 14 minutes before, 2 of
        them passing the threshold too high to land.

        ``above`` (stack_moves): an aircraft above the lowest in its stack, at least RELEASE_CLEAR_NM from every
        aircraft below it there, as real controllers take an aircraft off a stack from any level when there is room.
        ``inbound`` (stack_moves): an aircraft on its way to its stack, not yet holding, straight onto its downwind,
        as real controllers bring a high arrival straight in. Both judge the height by the real descent rates for
        each height (RulesController._descent_s, measured), as the simulated pilots descend."""
        rules = self.rules
        by_cs = {v.cs: v for v in views}
        side = self._release_check(cs, by_cs, above, inbound)
        if side is None:
            return False
        p = rules.plans[cs]
        if rules.released_order:
            rules.behind_released(p, by_cs)
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
        p.min_join = p.d_join  # ... and with downwind="fresh", not below this
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
    elif kind == "release_above":
        ctl.release_now(cs, env.session.world.views(), above=True)
    elif kind == "straight_in":
        ctl.release_now(cs, env.session.world.views(), inbound=True)
    return action


def redraw_pilots(world, seed: int) -> None:
    """Draw every pilot again from the measured spread, and new response delays: how a copy's crews fly.

    With drifting pilots, a crew in ``world.known_places`` (set by a LookAhead with known_copies: the place in the
    spread it was last seen at, and when) is drawn from where that puts it now, as a drifting crew moves on from a
    known place (approach.sim.world.drift_start, then drift_step over the time since), instead of from the whole
    spread."""
    from approach.sim.world import (_BOUNDS, FINAL_SLOWING_BOUNDS, FPM_TO_MS, descent_by_height, descent_profile,
                                    drift_start, drift_step, expedite_profile,
                                    descent_rate_at, draw, final_slowing_quantiles, response_quantiles)

    world.rng = random.Random(seed)
    world.pilot_rng = random.Random(seed * 7919 + 17)
    world.drift_rng = random.Random(seed * 104729 + 31)
    if not world.measured:
        return
    q = response_quantiles(world.airport.icao)
    table = final_slowing_quantiles(world.airport.icao)
    by_height = bool(descent_by_height(world.airport.icao))
    alts = {v.cs: v.alt_ft for v in world.views()} if by_height else {}
    known = (getattr(world, "known_places", None) or {}) if world.pilot_model in ("drifting", "drifting_handover") \
        else {}
    for cs, p in world.pilots.items():
        for name in _BOUNDS:
            u = world.pilot_rng.random()
            setattr(p, name, draw(q[name], u, _BOUNDS[name]))
            if name == "descent_fpm" and by_height and cs in alts:
                start = None
                if cs in known:  # from the place it was last seen at, moved on as a drifting crew's would be
                    u_seen, t_seen = known[cs]
                    lasting, z = drift_start(u_seen, world.drift_rng)
                    u = u_seen
                    if world.t > t_seen:
                        z, u = drift_step(lasting, z, world.t - t_seen, world.drift_rng)
                    start = (lasting, z)
                p.descent_u = u
                p.descent_profile = descent_profile(world.airport.icao, u)
                p.expedite_profile = expedite_profile(world.airport.icao, u)
                p.descent_fpm = descent_rate_at(world.airport.icao,
                                                p.expedite_profile if p.expedite else p.descent_profile, alts[cs])
                if world.pilot_model in ("drifting", "drifting_handover"):  # drawn again, then drifting
                    p.drift_lasting, p.drift_z = start if start else drift_start(u, world.drift_rng)
                    p.drift_t = world.t
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
                 moves: str = "small", time_weight: float = 0.0, releases: bool = False,
                 go_around_extra: float = 0.0, stack_moves: bool = False, careful_stacks: bool = False,
                 known_copies: bool = False):
        """``time_weight`` (efficient look-ahead): each aircraft-minute in the sector during the copies, and each
        minute the planner still expects aircraft to need at their end, costs this much in the score (a loss of
        separation costs 5). ``releases``: also try taking the lowest aircraft of each stack off it now.
        ``stack_moves`` (with releases): also try taking an aircraft off a stack above the lowest when it is clear
        of those below, and bringing an aircraft on its way to a stack straight in (release_now), with every
        new move judged over STACK_MOVES_HORIZON_S. ``careful_stacks``: holding an aircraft at another level in its
        stack, and taking that back, are made only where every copy shows no new trouble, as the efficiency moves
        are (CAREFUL_STACK_MOVES). ``known_copies`` (with drifting pilots): in the copies, each crew starts from what a
        controller has seen of its descent, the rate it was last seen descending at on radar or, with
        "drifting_handover" pilots, its rate at handover, moved on as a drifting crew's would be since
        (redraw_pilots); without it every crew is drawn from the whole spread at every look."""
        if moves not in ("small", "wide"):
            raise ValueError("moves must be small or wide")
        self.workers, self.draws, self.min_gain, self.moves = workers, draws, min_gain, moves
        self.time_weight, self.releases, self.go_around_extra = time_weight, releases, go_around_extra
        self.stack_moves = stack_moves and releases
        self.careful_stacks = careful_stacks
        self.known_copies = known_copies
        self.last_seen: dict[str, tuple[float, float]] = {}  # known_copies: cs -> (place seen descending, when)
        self.release_horizon_s = RELEASE_HORIZON_S
        self.horizon_s = HORIZON_WIDE_S if moves == "wide" else HORIZON_S
        self.gap_weight = GAP_WEIGHT if moves == "wide" else 0.0
        self.rng = random.Random(seed)
        self.last_look = -1e9
        self.made: dict[tuple, float] = {}  # when each move in force was made: (cs, dimension or "stack") -> t
        self.release_tried: dict[tuple, float] = {}  # stack_moves: when each release was last tried, (kind, cs) -> t
        self.stats = Counter()
        self.changes: list[dict] = []

    def reset(self) -> None:
        self.last_look = -1e9
        self.made = {}
        self.release_tried = {}
        self.last_seen = {}

    def know(self, world) -> None:
        """known_copies: note where each aircraft seen descending lies in the spread now, and give the world each
        aircraft's latest known place and when (world.known_places, read by redraw_pilots in the copies): from radar,
        or, with "drifting_handover" pilots, from its rate at handover if that is later."""
        from approach.response import seen_descent_u

        views = world.views()
        for v, u in zip(views, seen_descent_u(views, world.airport.icao)):
            if np.isfinite(u):
                self.last_seen[v.cs] = (float(u), world.t)
        known = {}
        for v in views:
            recs = [self.last_seen.get(v.cs)]
            if world.pilot_model == "drifting_handover":
                recs.append(getattr(world, "handover_seen", {}).get(v.cs))
            recs = [r for r in recs if r is not None]
            if recs:
                known[v.cs] = max(recs, key=lambda r: r[1])
        world.known_places = known

    def score(self, copy: dict) -> float:
        time = copy.get("aircraft_minutes", 0.0) + copy.get("remaining_minutes", 0.0)
        return (copy["reward"] - self.gap_weight * len(copy.get("gaps", [])) - self.time_weight * time
                - self.go_around_extra * len(copy.get("go_arounds", [])))

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
            if self.releases:  # the holding aircraft next in the planner's queue, off its stack now
                from approach.controllers.rules import STACKS

                lows = []
                for fix in STACKS:
                    members = [p for p in rules._stack(fix, views) if p.holding and p.cs in views]
                    if members:
                        low = min(members, key=lambda p: views[p.cs].alt_ft)
                        if not rules._anyone_below_in_stack(low, views):
                            lows.append(low)
                if lows:
                    out.append(("release_now", min(lows, key=lambda p: p.landing_t).cs))
            if self.stack_moves:  # off a stack above the lowest, and straight in: the one next to land of each
                listed = list(views.values())
                aboves = []
                for fix in STACKS:
                    members = sorted((p for p in rules._stack(fix, views) if p.holding and p.cs in views),
                                     key=lambda p: views[p.cs].alt_ft)
                    aboves += [p for p in members[1:2] if ctl.releasable(p.cs, listed, above=True)]
                if aboves:
                    out.append(("release_above", min(aboves, key=lambda p: p.landing_t).cs))
                coming = [p for p in rules.plans.values() if p.stage == "inbound" and not p.holding and p.cs in views
                          and ctl.releasable(p.cs, listed, inbound=True)]
                if coming:
                    out.append(("straight_in", min(coming, key=lambda p: p.landing_t).cs))
                now = env.session.t
                out = [c for c in out if c[0] not in NEW_STACK_MOVES
                       or now - self.release_tried.get(tuple(c[:2]), -1e9) >= RELEASE_RETRY_S]
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
        if self.known_copies:
            self.know(env.session.world)
        seeds = [self.rng.randrange(1 << 30) for _ in range(self.draws)]
        self.last_base = fly_copies(env, [(s, None) for s in seeds], self.workers, self.horizon_s)  # one per seed
        base = [b for b in self.last_base if "error" not in b]
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

    def _no_new_trouble(self, copies: list[dict], base: list[dict] | None = None) -> bool:
        """An efficiency move (EFFICIENCY_MOVES) is made only if, in every copy, it brings no more losses of
        separation, go-arounds or gaps inside the minimum than the same copy (the same pilots) without it: time is
        saved only where it costs no safety. Without this, on 9 of the 12 dev training sessions the look-ahead with
        releases and a time weight of 0.1 had 7 go-arounds (a go-around costs 3, and a release that saves ten
        aircraft two minutes each scores 2)."""
        def trouble(c):
            return len(c.get("losses", [])) + len(c.get("go_arounds", [])) + len(c.get("gaps", []))
        base = getattr(self, "last_base", []) if base is None else base
        for c, b in zip(copies, base):
            if "error" in c or "error" in b or trouble(c) > trouble(b):
                return False
        return len(copies) == len(base)

    def _look(self, env: VectorEnv, obs: np.ndarray, t: float) -> tuple | None:
        seeds, base, involved = self.probe(env)
        if not base:
            return None
        if involved:
            self.stats["looks_with_a_problem"] += 1
        cands = self.candidates(env, obs, involved)
        if not cands:
            return None
        best, best_gain = None, None
        for group, horizon in ((tuple(c for c in cands if c[0] not in EFFICIENCY_MOVES), self.horizon_s),
                               (tuple(c for c in cands if c[0] == "release_now"), self.release_horizon_s),
                               (tuple(c for c in cands if c[0] in NEW_STACK_MOVES), STACK_MOVES_HORIZON_S)):
            if not group:
                continue
            ref = base
            if horizon != self.horizon_s:  # copies with nothing changed over the longer horizon, the same pilots
                ref_all = fly_copies(env, [(s, None) for s in seeds], self.workers, horizon)
                self.stats["copies"] += len(seeds)
                ref = [b for b in ref_all if "error" not in b]
                if len(ref) < len(seeds):
                    continue
            else:
                ref_all = self.last_base
            jobs = [(s, cand) for cand in group for s in seeds]
            for cand in group:
                if cand[0] in NEW_STACK_MOVES:
                    self.release_tried[tuple(cand[:2])] = t
            res = fly_copies(env, jobs, self.workers, horizon)
            self.stats["copies"] += len(jobs)
            base_score = float(np.mean([self.score(b) for b in ref]))
            for k, cand in enumerate(group):
                mine = res[k * len(seeds):(k + 1) * len(seeds)]
                rs = [r for r in mine if "error" not in r]
                if len(rs) < len(seeds):
                    continue
                gain = float(np.mean([self.score(r) for r in rs])) - base_score
                if ((cand[0] in EFFICIENCY_MOVES or (self.careful_stacks and cand[0] in CAREFUL_STACK_MOVES))
                        and not self._no_new_trouble(mine, ref_all)):
                    self.stats["vetoed"] += 1
                    continue
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
        elif best[0] in ("release_now", "release_above", "straight_in"):
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


MOVE_KINDS = ("set", "set_planned", "hold_level", "release", "extend", "release_now", "release_above", "straight_in")
EFFICIENCY_MOVES = ("release_now", "release_above", "straight_in")  # made only where every copy shows no new trouble (LookAhead._no_new_trouble)
# careful_stacks: these too. In the 120 test sessions on the simulator with descents by height (18 November), 5 of the
# look-ahead's 9 losses of separation at a stack came within 3 minutes of holding one of the pair at another level in
# its stack, or taking that back: a climb or descent past an aircraft just under 3 NM away, with 800 to 900 ft between
# them. Neither move had to pass the veto, and taking a hold back needed no gain at all.
CAREFUL_STACK_MOVES = ("hold_level", "release")


def _run(args: tuple) -> list[dict]:
    sessions, draws, min_gain, with_competitor, moves, copy, parts, time_weight, releases, go_around_extra, planner = args[:11]
    stack_moves = bool(args[11]) if len(args) > 11 else False
    copy_workers = int(args[12]) if len(args) > 12 else 1
    careful_stacks = bool(args[13]) if len(args) > 13 else False
    opts = args[-1] if isinstance(args[-1], dict) else {}  # options by name, last
    pilots = opts.get("pilots", "measured")
    env = LookAheadEnv(hours=[s["hour"] for s in sessions], planner=planner or None, pilots=pilots,
                       go_arounds=opts.get("go_arounds", "published"))
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
                la = LookAhead(workers=copy_workers, draws=draws, min_gain=min_gain, seed=s["pilot_seed"], moves=moves,
                               time_weight=time_weight, releases=releases, go_around_extra=go_around_extra,
                               stack_moves=stack_moves, careful_stacks=careful_stacks,
                               known_copies=bool(opts.get("known_copies")))
            row, secs = fly_session(env, options, la)
            rows.append({**meta, "controller": "ai", **row, "wall_s": round(secs),
                         "looks": la.stats["looks"], "looks_with_a_problem": la.stats["looks_with_a_problem"],
                         "copies": la.stats["copies"], "changes": la.stats["changes"],
                         "back_to_plan": la.stats["back_to_plan"],
                         **{f"moves_{k}": la.stats[k] for k in MOVE_KINDS},
                         "look_s": round(la.stats["look_seconds"], 1), "vetoed": la.stats["vetoed"],
                         "change_log": json.dumps(la.changes)})
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
    ap.add_argument("--copy-workers", type=int, default=1,
                    help="copies flown at once within each session (the copies are seeded and their results kept in "
                         "order, so the choices are the same): with --workers 1 and 4 here, each session finishes "
                         "sooner and a machine restart loses less")
    ap.add_argument("--seed", type=int, default=2026)
    ap.add_argument("--draws", type=int, default=DRAWS)
    ap.add_argument("--min-gain", type=float, default=MIN_GAIN)
    ap.add_argument("--moves", default="small", choices=("small", "wide"))
    ap.add_argument("--train", action="store_true",
                    help="training hours instead of the test's (for trying settings without looking at the test)")
    ap.add_argument("--time-weight", type=float, default=0.0,
                    help="each aircraft-minute (in the copies, and still expected at their end) costs this much")
    ap.add_argument("--go-around-extra", type=float, default=0.0,
                    help="each go-around in the copies costs this much more than the training world's 3")
    ap.add_argument("--releases", action="store_true",
                    help="also try taking the lowest aircraft of each stack off it now (the planner would wait)")
    ap.add_argument("--stack-moves", action="store_true",
                    help="with --releases: also try taking an aircraft off a stack above the lowest when it is clear "
                         "of those below, and bringing an aircraft on its way to a stack straight in")
    ap.add_argument("--careful-stacks", action="store_true",
                    help="hold an aircraft at another level in its stack, or take that back, only where every copy "
                         "shows no new trouble (as for the releases)")
    ap.add_argument("--known-copies", action="store_true",
                    help="with drifting pilots: in the copies, each crew starts from the rate it was last seen "
                         "descending at (or its rate at handover, with drifting_handover pilots)")
    ap.add_argument("--planner", default="",
                    help='changes to the planner (vector.PLANNER_2), as JSON, e.g. \'{"height_by": "time"}\': for the '
                         "competitor and the look-ahead alike")
    ap.add_argument("--copy", default="",
                    help="fly the network that copies the look-ahead (a saved approach.ai.imitate model) instead")
    ap.add_argument("--pilots", default="measured", choices=("measured", "drifting", "drifting_handover"),
                    help="how the simulated pilots fly: \"drifting\" pilots' descent rates wander as real crews' do "
                         "(approach.sim.world), for the competitor, the look-ahead and its copies alike; "
                         "\"drifting_handover\" ones start where their real crews were at handover")
    ap.add_argument("--go-arounds", default="published", choices=GO_AROUNDS,
                    help="how the simulated go-arounds are flown: \"no_descent\" ones sent round from above 3,000 ft "
                         "level off instead of coming down to it (approach.sim.world)")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    planner = json.loads(args.planner) if args.planner else {}
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
    saved = list(csv.DictReader(cache.open())) if cache.is_file() and not args.train and not planner else []
    print(f"{'Reusing' if saved else 'Flying'} the competitor's results{' from ' + cache.name if saved else ''}", flush=True)
    # Each session's rows are saved as it finishes, so a run stopped part way (the machine restarted) carries on
    # from where it was when started again with the same arguments.
    parts = Path(f"{args.out}.parts")
    parts.mkdir(parents=True, exist_ok=True)
    # One session a task, handed to whichever process is free (dealt out in a fixed order, one process could be left
    # with several busy hours while the others sat idle).
    tasks = [([s], args.draws, args.min_gain, not saved, args.moves, args.copy, parts, args.time_weight, args.releases,
              args.go_around_extra, planner, args.stack_moves, args.copy_workers, args.careful_stacks,
              {"pilots": args.pilots, "go_arounds": args.go_arounds, "known_copies": args.known_copies})
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
    la = LookAhead(moves=args.moves, releases=args.releases, stack_moves=args.stack_moves,
                   careful_stacks=args.careful_stacks)
    summary = {"controller": "copy" if args.copy else "lookahead", **({"copy": args.copy} if args.copy else {}),
               "moves": args.moves, "hours": "train" if args.train else "test",
               "sessions": len(flown), "seed": args.seed, "draws": args.draws, "min_gain": args.min_gain,
               "look_every_s": LOOK_EVERY_S, "horizon_s": la.horizon_s, "gap_weight": la.gap_weight,
               "time_weight": args.time_weight, "releases": args.releases, "go_around_extra": args.go_around_extra,
               "planner": planner,
               **({"pilots": args.pilots} if args.pilots != "measured" else {}),
               **({"go_arounds": args.go_arounds} if args.go_arounds != "published" else {}),
               "quick_copies": QUICK_COPIES,
               "changes": sum(int(r["changes"]) for r in ai), "back_to_plan": sum(int(r["back_to_plan"]) for r in ai),
               **{f"moves_{k}": sum(int(r.get(f"moves_{k}") or 0) for r in ai) for k in MOVE_KINDS},
               "copies": sum(int(r["copies"]) for r in ai), "looks": looks,
               "looks_with_a_problem": sum(int(r["looks_with_a_problem"]) for r in ai),
               "releases_vetoed": sum(int(r.get("vetoed") or 0) for r in ai),
               "release_horizon_s": la.release_horizon_s, "stack_moves": la.stack_moves,
               **({"careful_stacks": True} if la.careful_stacks else {}),
               **({"known_copies": True} if args.known_copies else {}),
               **({"stack_moves_horizon_s": STACK_MOVES_HORIZON_S, "stack_moves_retry_s": RELEASE_RETRY_S}
                  if la.stack_moves else {}),
               "seconds_per_look": round(sum(float(r["look_s"]) for r in ai) / looks, 1),
               "minutes_per_session": round(sum(int(r["wall_s"]) for r in ai) / 60 / max(len(ai), 1), 1),
               **summarise(rows)}
    Path(f"{base}.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
