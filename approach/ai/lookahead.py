"""Stage 2's look-ahead controller: it tries changes in copies of the simulator before giving them.

Every LOOK_EVERY_S it flies the next HORIZON_S forward from the current moment in copies of the simulator (forked
processes), keeping every adjustment in force (with nothing in force that is the planner's own clearances), DRAWS
times. In each copy every pilot is drawn again from the measured spread (approach.sim.world, pilots="measured"),
and so are the pilots' response delays: a controller does not know how a particular crew will fly. If any copy
shows a loss of separation or a go-around, it flies the same copies again with each single change it could make to
the aircraft involved (and, for a go-around, the aircraft ahead of it): a heading 5 or 10 degrees either way, a level
1,000 ft up or down, a speed 10 or 20 kt either way, or back to as planned for a change already in force. Each
change is held through the copy. It gives the change whose copies score best, if they score at least MIN_GAIN
better than doing nothing; the score is the training world's reward over the copies' minutes (5 for each loss of
separation, 3 for each go-around or wake infringement, 1 for each landing, and the small terms for gaps, profile,
instructions and time in the sector). A change in force is also tried back to as planned at every look, and taken
back when that scores no worse. One change at a time: at most one per look.

What the copies know is what a controller would: the traffic in the sector and the arrivals due in the next few
minutes (the scenario's real arrivals spawn in the copies at their real times, as their flight strips would show),
the wind, the planner's plan, and the safety check. They do not know how each crew will fly.

The changes are the same as stage 2's AI makes (approach.ai.vector, hold=True), so the look-ahead controller flies
in VectorEnv and its sessions compare like for like with the competitor's ("as planned" everywhere).

    PYTHONPATH=. python -m approach.ai.lookahead --sessions 12 --workers 4 --out runs/lookahead/first12
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

from approach.ai.vector import FEATURES_2, HOLD_CHOICES, KEEP, N_MAX, OFFSETS, PLANNED, VectorEnv

LOOK_EVERY_S = 30
HORIZON_S = 300
DRAWS = 3
MIN_GAIN = 1.0  # a change must score this much better than doing nothing (a fifth of a loss of separation)
CAN = [FEATURES_2.index(f) for f in ("can_heading", "can_level", "can_speed")]
DIMS = ("heading", "level", "speed")


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
    """Record each new loss of separation (the pair) and each go-around, in this copy."""
    conflicts, advance = type(env)._conflicts.__get__(env), type(env)._advance.__get__(env)

    def _conflicts():
        old = set(env.in_conflict)
        r = conflicts()
        log["losses"] += [list(pair) for pair in sorted(env.in_conflict - old)]
        return r

    def _advance(seconds):
        r, events = advance(seconds)
        log["go_arounds"] += [e["cs"] for e in events if e["kind"] == "go_around"]
        return r, events

    env._conflicts, env._advance = _conflicts, _advance


def _copy(env: VectorEnv, seed: int, action: np.ndarray) -> dict:
    """In a forked child: fly HORIZON_S on with fresh pilots, the given choices now and keep after."""
    redraw_pilots(env.session.world, seed)
    log = {"losses": [], "go_arounds": []}
    _instrument(env, log)
    t_end, total, done, first = env.session.t + HORIZON_S, 0.0, False, True
    keep = np.zeros_like(action)
    while not done and env.session.t < t_end:
        _, r, done, _, _ = env.step(action if first else keep)
        first = False
        total += float(r)
    return {"reward": total, **log}


def fly_copies(env: VectorEnv, jobs: list[tuple[int, np.ndarray]], workers: int = 1) -> list[dict]:
    """Fly each (pilot seed, choices) from the current moment in its own forked copy of the simulator."""
    out, running, queue = [None] * len(jobs), {}, list(enumerate(jobs))
    while queue or running:
        while queue and len(running) < workers:
            i, (seed, action) = queue.pop(0)
            r, w = os.pipe()
            pid = os.fork()
            if pid == 0:  # the copy
                os.close(r)
                try:
                    res = _copy(env, seed, action)
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
    """Chooses stage 2's adjustments (hold=True) by trying them in copies of the simulator."""

    def __init__(self, workers: int = 1, draws: int = DRAWS, min_gain: float = MIN_GAIN, seed: int = 0):
        self.workers, self.draws, self.min_gain = workers, draws, min_gain
        self.rng = random.Random(seed)
        self.last_look = -1e9
        self.stats = Counter()
        self.changes: list[dict] = []

    def reset(self) -> None:
        self.last_look = -1e9

    def candidates(self, env: VectorEnv, obs: np.ndarray, involved: set[str]) -> list[tuple[str, int, int]]:
        """(aircraft, kind of adjustment, choice): back to as planned for each change in force, and every other
        choice for each aircraft involved, where the AI can adjust it now."""
        out = []
        for i, cs in enumerate(env.slots):
            now = env.vector.offsets.get(cs, (0, 0, 0))
            for b in range(3):
                if obs[i, CAN[b]] < 0.5:
                    continue
                if now[b] != 0:
                    out.append((cs, b, PLANNED))
                if cs in involved:
                    out += [(cs, b, PLANNED + k) for k in range(1, len(OFFSETS[b])) if k != now[b]]
        return out

    def choose(self, env: VectorEnv, obs: np.ndarray) -> np.ndarray:
        """The choices for this step: keep everything, except at a look, when one change may be made."""
        action = np.zeros(3 * N_MAX, dtype=np.int64)  # KEEP everywhere
        t = env.session.t
        if t - self.last_look < LOOK_EVERY_S:
            return action
        self.last_look = t
        t0 = time.time()
        seeds = [self.rng.randrange(1 << 30) for _ in range(self.draws)]
        base = fly_copies(env, [(s, action) for s in seeds], self.workers)
        base = [b for b in base if "error" not in b]
        self.stats["looks"] += 1
        self.stats["copies"] += len(seeds)
        if not base:
            return action
        involved = set()
        for b in base:
            for pair in b["losses"]:
                involved.update(pair)
            for cs in b["go_arounds"]:
                involved.add(cs)
                if cs in env.slots and env.slots.index(cs) > 0:
                    involved.add(env.slots[env.slots.index(cs) - 1])  # the aircraft ahead of it
        if involved:
            self.stats["looks_with_a_problem"] += 1
        cands = self.candidates(env, obs, involved)
        if not cands:
            return action
        jobs = []
        for cs, b, choice in cands:
            a = action.copy()
            a[b * N_MAX + env.slots.index(cs)] = choice
            jobs += [(s, a) for s in seeds]
        res = fly_copies(env, jobs, self.workers)
        self.stats["copies"] += len(jobs)
        base_score = float(np.mean([b["reward"] for b in base]))
        best, best_gain = None, None
        for k, cand in enumerate(cands):
            rs = [r for r in res[k * len(seeds):(k + 1) * len(seeds)] if "error" not in r]
            if len(rs) < len(seeds):
                continue
            gain = float(np.mean([r["reward"] for r in rs])) - base_score
            need = 0.0 if cand[2] == PLANNED else self.min_gain
            if gain >= need and (best_gain is None or gain > best_gain):
                best, best_gain = cand, gain
        self.stats["look_seconds"] += time.time() - t0
        if best is None:
            return action
        cs, b, choice = best
        action[b * N_MAX + env.slots.index(cs)] = choice
        offset = 0 if choice == PLANNED else OFFSETS[b][choice - 1]
        self.stats["back_to_plan" if choice == PLANNED else "changes"] += 1
        self.changes.append({"t": round(t), "aircraft": cs, "kind": DIMS[b], "offset": offset,
                             "gain": round(best_gain, 2), "problem": bool(involved)})
        return action


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


def _run(args: tuple) -> list[dict]:
    sessions, draws, min_gain, with_competitor = args
    env = VectorEnv(hours=[s["hour"] for s in sessions], hold=True)
    rows = []
    for s in sessions:
        options = {"hour": s["hour"], "offset_s": s["offset_s"], "pilot_seed": s["pilot_seed"]}
        meta = {"session": s["id"], "hour": f"{s['hour'][0]}T{s['hour'][1]:02d}", "offset_min": round(s["offset_s"] / 60)}
        try:
            if with_competitor:
                row, secs = fly_session(env, {**options, "teacher": True}, None)
                rows.append({**meta, "controller": "rules_vector", **row})
            la = LookAhead(workers=1, draws=draws, min_gain=min_gain, seed=s["pilot_seed"])
            row, secs = fly_session(env, options, la)
            rows.append({**meta, "controller": "ai", **row, "wall_s": round(secs),
                         "looks": la.stats["looks"], "looks_with_a_problem": la.stats["looks_with_a_problem"],
                         "copies": la.stats["copies"], "changes": la.stats["changes"],
                         "back_to_plan": la.stats["back_to_plan"],
                         "look_s": round(la.stats["look_seconds"], 1), "change_log": json.dumps(la.changes)})
            print(f"session {s['id']}: {row['separation_losses']} losses of separation, {row['go_arounds']} go-arounds, "
                  f"{la.stats['changes']} changes, {la.stats['copies']} copies, {secs / 60:.1f} min", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"session {s['id']} {s['hour']} skipped: {e!r}", flush=True)
    return rows


def main() -> None:
    import csv
    from multiprocessing import Pool
    from pathlib import Path

    from approach.ai.evaluate import (COMPETITOR_DIR, _fields, code_hash, real_rows, summarise, test_hours)

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sessions", type=int, default=120, help="the first N of the test's 120 sessions")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--seed", type=int, default=2026)
    ap.add_argument("--draws", type=int, default=DRAWS)
    ap.add_argument("--min-gain", type=float, default=MIN_GAIN)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    rng = random.Random(args.seed)
    hours = test_hours()
    sessions = [{"id": k, "hour": rng.choice(hours), "offset_s": rng.uniform(0, 3600), "pilot_seed": rng.randrange(1 << 30)}
                for k in range(120)][:args.sessions]  # the test's own sessions, in its order
    cache = COMPETITOR_DIR / f"2_seed{args.seed}_n120_{code_hash()}.csv"
    saved = list(csv.DictReader(cache.open())) if cache.is_file() else []
    print(f"{'Reusing' if saved else 'Flying'} the competitor's results{' from ' + cache.name if saved else ''}", flush=True)
    chunks = [(sessions[k::args.workers], args.draws, args.min_gain, not saved) for k in range(args.workers)]
    with Pool(args.workers) as pool:
        rows = [r for part in pool.map(_run, chunks) for r in part]
    flown = {int(r["session"]) for r in rows if r["controller"] == "ai"}
    if saved:
        rows += [r for r in saved if int(r["session"]) in flown]
    else:
        rows += [r for r in real_rows([s for s in sessions if s["id"] in flown])]
    rows.sort(key=lambda r: (int(r["session"]), r["controller"]))
    base = Path(args.out)
    base.parent.mkdir(parents=True, exist_ok=True)
    with Path(f"{base}.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=_fields(rows))
        w.writeheader()
        w.writerows(rows)
    ai = [r for r in rows if r["controller"] == "ai"]
    summary = {"controller": "lookahead", "sessions": len(flown), "seed": args.seed, "draws": args.draws,
               "min_gain": args.min_gain, "look_every_s": LOOK_EVERY_S, "horizon_s": HORIZON_S,
               "changes": sum(int(r["changes"]) for r in ai), "back_to_plan": sum(int(r["back_to_plan"]) for r in ai),
               "copies": sum(int(r["copies"]) for r in ai), "looks": sum(int(r["looks"]) for r in ai),
               "looks_with_a_problem": sum(int(r["looks_with_a_problem"]) for r in ai),
               "seconds_per_look": round(sum(float(r["look_s"]) for r in ai) / max(sum(int(r["looks"]) for r in ai), 1), 1),
               "minutes_per_session": round(sum(int(r["wall_s"]) for r in ai) / 60 / max(len(ai), 1), 1),
               **summarise(rows)}
    Path(f"{base}.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
