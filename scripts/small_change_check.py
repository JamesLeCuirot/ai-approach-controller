"""Can one small change avoid the competitor's losses of separation and go-arounds?

Stage 2's AI adjusts the planner's clearances by small steps (heading 5 or 10 degrees either way, level 1,000 ft up or
down, speed 10 or 20 kt either way), and only for aircraft on downwind, base or cleared to land. Run stage2-01 never
moved from "as planned". This check asks whether there is anything to find: it flies the competitor (as planned) on
training hours, notes each loss of separation and each go-around, then goes back 1 to 4 minutes before each one and
tries every single change the AI could make to the aircraft involved, held until the moment of the event, then as
planned again. Each try runs to 10 minutes after the event and is compared with an unchanged run from the same moment.

The simulator's state at a branch moment is copied by forking the process, so each try only flies the minutes after
it. Test hours are not used.

Usage: PYTHONPATH=. python scripts/small_change_check.py --sessions 40 --workers 4 --out runs/stage2-checks/small_changes
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
from collections import Counter
from pathlib import Path

import numpy as np

from approach.ai.vector import CHOICES, FEATURES_2, HEADING_OFFSETS, LEVEL_OFFSETS, N_MAX, SPEED_OFFSETS, VectorEnv
from approach.ai.vector import as_planned

LEADS_S = (60, 120, 180, 240)  # how long before the event the change is made (and held)
AFTER_S = 600  # each try runs on to this long after the event
CAN = [FEATURES_2.index(f) for f in ("can_heading", "can_level", "can_speed")]
DIMS = ("heading", "level", "speed")
OFFSETS = (HEADING_OFFSETS, LEVEL_OFFSETS, SPEED_OFFSETS)
ADJUSTABLE = ("downwind", "base", "cleared")


def instrument(env: VectorEnv, log: dict) -> None:
    """Record each new loss of separation (time, pair, the stages of the two aircraft), go-around and landing."""
    conflicts, advance = type(env)._conflicts.__get__(env), type(env)._advance.__get__(env)  # replace, don't stack

    def _conflicts():
        old = set(env.in_conflict)
        r = conflicts()
        for pair in sorted(env.in_conflict - old):
            stages = [env.controller.stage_of(cs) for cs in pair]
            log["losses"].append({"t": env.session.t, "pair": list(pair), "stages": stages,
                                  "at_stack": any(s == "inbound" for s in stages)})
        return r

    def _advance(seconds):
        r, events = advance(seconds)
        for e in events:
            if e["kind"] in ("go_around", "landed"):
                log[e["kind"]].append({"t": env.session.t, "cs": e["cs"]})
        return r, events

    env._conflicts, env._advance = _conflicts, _advance


def new_log() -> dict:
    return {"losses": [], "go_around": [], "landed": []}


def action_for(env: VectorEnv, change: tuple[str, int, int] | None) -> np.ndarray:
    """As planned, with one aircraft's choice changed (if it still has a place in the AI's picture)."""
    act = as_planned()
    if change is not None:
        cs, dim, choice = change
        if cs in env.slots:
            act[dim * N_MAX + env.slots.index(cs)] = choice
    return act


def window(log: dict, t0: float, t1: float) -> dict:
    losses = [x for x in log["losses"] if t0 <= x["t"] <= t1]
    return {"losses": len(losses), "losses_away": sum(1 for x in losses if not x["at_stack"]),
            "go_arounds": sum(1 for x in log["go_around"] if t0 <= x["t"] <= t1),
            "landed": sum(1 for x in log["landed"] if t0 <= x["t"] <= t1)}


def happened(log: dict, event: dict, t0: float, t1: float) -> bool:
    if event["kind"] == "loss":
        return any(x["pair"] == event["pair"] and t0 <= x["t"] <= t1 for x in log["losses"])
    return any(x["cs"] == event["cs"] and t0 <= x["t"] <= t1 for x in log["go_around"])


def run_try(env: VectorEnv, change, hold_until: float, end: float) -> dict:
    """Fly on from the current state with one change held until ``hold_until``; called in a forked child."""
    log = new_log()
    instrument(env, log)
    t0, blocked0, own0 = env.session.t, env.vector.ai_blocked, env.counts["instructions"]
    done = False
    while not done and env.session.t < end:
        _, _, done, _, _ = env.step(action_for(env, change if env.session.t < hold_until else None))
    return {"log": log, "blocked": env.vector.ai_blocked - blocked0, "own": env.counts["instructions"] - own0,
            "t0": t0, "t1": env.session.t}


def fork_tries(env: VectorEnv, tries: list[tuple], workers: int) -> list[dict]:
    """Run each (change, hold_until, end) from the current state in its own forked process."""
    out, running = [None] * len(tries), {}
    queue = list(enumerate(tries))
    while queue or running:
        while queue and len(running) < workers:
            i, (change, hold_until, end) = queue.pop(0)
            r, w = os.pipe()
            pid = os.fork()
            if pid == 0:  # child
                os.close(r)
                try:
                    res = run_try(env, change, hold_until, end)
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


def first_pass(env: VectorEnv, options: dict) -> tuple[dict, list[float]]:
    """Fly the session as planned; return what happened and the time of each decision."""
    log = new_log()
    env.reset(options=options)
    instrument(env, log)
    times, done = [], False
    while not done:
        times.append(env.session.t)
        _, _, done, _, _ = env.step(as_planned())
    return log, times


def targets(log: dict) -> list[dict]:
    out = [{"kind": "loss", "t": x["t"], "pair": x["pair"], "at_stack": x["at_stack"], "stages": x["stages"],
            "aircraft": x["pair"]} for x in log["losses"]]
    out += [{"kind": "go_around", "t": x["t"], "cs": x["cs"], "aircraft": [x["cs"]]} for x in log["go_around"]]
    return out


def second_pass(env: VectorEnv, options: dict, events: list[dict], times: list[float], workers: int) -> list[dict]:
    """Fly the session again as planned; at each branch moment, fork the tries for the events it serves."""
    branch_at: dict[float, list[tuple[dict, int]]] = {}
    for ev in events:
        for lead in LEADS_S:
            moment = max((t for t in times if t <= ev["t"] - lead), default=None)
            if moment is not None:
                branch_at.setdefault(moment, []).append((ev, lead))
    obs, _ = env.reset(options=options)
    results, done = [], False
    while not done:
        t = env.session.t
        for ev, lead in branch_at.get(t, []):
            by_cs = {cs: i for i, cs in enumerate(env.slots)}
            tries, labels = [(None, ev["t"], ev["t"] + AFTER_S)], [("as planned", None, None, 0)]
            stages = {cs: env.controller.stage_of(cs) for cs in ev["aircraft"]}
            for cs in ev["aircraft"]:
                if cs not in by_cs:
                    continue
                row = obs[by_cs[cs]]
                for d in range(3):
                    if row[CAN[d]] <= 0.5:
                        continue
                    for choice in range(1, CHOICES[d]):
                        tries.append(((cs, d, choice), ev["t"], ev["t"] + AFTER_S))
                        labels.append((cs, DIMS[d], OFFSETS[d][choice], choice))
            row_out = {"event": ev, "lead_s": lead, "branch_t": t, "stages_at_branch": stages, "tries": []}
            if len(tries) == 1:  # nothing the AI could change: no need to fly it
                results.append(row_out)
                continue
            res = fork_tries(env, tries, workers)
            base = res[0]
            row_out["control_reproduced"] = "log" in base and happened(base["log"], ev, t, ev["t"] + 5)
            if "log" not in base:
                row_out["error"] = base.get("error")
                results.append(row_out)
                continue
            b = window(base["log"], t, ev["t"] + AFTER_S)
            row_out["control"] = b
            for (cs, dim, offset, _), r in zip(labels[1:], res[1:]):
                if "log" not in r:
                    continue
                w = window(r["log"], t, ev["t"] + AFTER_S)
                row_out["tries"].append({
                    "aircraft": cs, "dim": dim, "offset": offset, "blocked": r["blocked"], "own": r["own"],
                    "avoided": not happened(r["log"], ev, t, ev["t"] + 30),
                    "d_losses": w["losses"] - b["losses"], "d_losses_away": w["losses_away"] - b["losses_away"],
                    "d_go_arounds": w["go_arounds"] - b["go_arounds"], "d_landed": w["landed"] - b["landed"]})
            results.append(row_out)
        obs, _, done, _, _ = env.step(as_planned())
    return results


def helps(tr: dict) -> bool:
    """The event is avoided and the 10 minutes after are no worse: no more losses of separation or go-arounds, and
    no fewer landings."""
    return tr["avoided"] and tr["d_losses"] <= 0 and tr["d_go_arounds"] <= 0 and tr["d_landed"] >= 0 and (
        tr["d_losses"] + tr["d_go_arounds"] < 0)


def harms(tr: dict) -> bool:
    return tr["d_losses"] > 0 or tr["d_go_arounds"] > 0 or tr["d_landed"] < 0


def summarise(rows: list[dict]) -> dict:
    ok = [r for r in rows if "control" in r]
    tries = [t for r in ok for t in r["tries"]]
    by_event: dict[tuple, list] = {}
    for r in rows:
        ev = r["event"]
        key = (r["session"], ev["kind"], ev["t"], tuple(ev["aircraft"]))
        by_event.setdefault(key, []).append(r)

    def kind_of(ev):
        if ev["kind"] == "go_around":
            return "go_around"
        return "loss_at_stack" if ev["at_stack"] else "loss_away"

    events = Counter()
    reachable, fixable, fixable_lead = Counter(), Counter(), Counter()
    for key, rs in by_event.items():
        k = kind_of(rs[0]["event"])
        events[k] += 1
        if any(r["tries"] for r in rs):
            reachable[k] += 1
        good = [(r["lead_s"], t) for r in rs for t in r["tries"] if helps(t)]
        if good:
            fixable[k] += 1
            fixable_lead[(k, min(lead for lead, _ in good))] += 1
    out = {
        "sessions": len({r["session"] for r in rows}),
        "branches": len(ok),
        "control_reproduced_pct": round(100 * np.mean([r["control_reproduced"] for r in ok]), 1) if ok else None,
        "events": dict(events),
        "events_with_an_aircraft_the_ai_can_adjust": dict(reachable),
        "events_one_change_fixes": dict(fixable),
        "earliest_fix_lead_s": {f"{k}@{lead}s": n for (k, lead), n in sorted(fixable_lead.items())},
        "tries": len(tries),
        "tries_blocked_pct": round(100 * np.mean([t["blocked"] > 0 for t in tries]), 1) if tries else None,
        "tries_that_help_pct": round(100 * np.mean([helps(t) for t in tries]), 1) if tries else None,
        "tries_that_harm_pct": round(100 * np.mean([harms(t) for t in tries]), 1) if tries else None,
        "mean_change_in_losses": round(float(np.mean([t["d_losses"] for t in tries])), 3) if tries else None,
        "by_change": {},
    }
    groups: dict[str, list] = {}
    for t in tries:
        groups.setdefault(f"{t['dim']} {t['offset']:+g}", []).append(t)
    for name, ts in sorted(groups.items()):
        out["by_change"][name] = {"tries": len(ts), "help_pct": round(100 * np.mean([helps(t) for t in ts]), 1),
                                  "harm_pct": round(100 * np.mean([harms(t) for t in ts]), 1),
                                  "avoided_pct": round(100 * np.mean([t["avoided"] for t in ts]), 1),
                                  "mean_d_losses": round(float(np.mean([t["d_losses"] for t in ts])), 3)}
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--sessions", type=int, default=40)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--seed", type=int, default=11)
    ap.add_argument("--out", default="runs/stage2-checks/small_changes")
    args = ap.parse_args()
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    env = VectorEnv(split="train", cached_winds_only=True, seed=args.seed)
    rng = random.Random(args.seed)
    hours = rng.sample(env.hours, min(args.sessions, len(env.hours)))
    rows_path = out.with_suffix(".jsonl")
    done_ids = set()
    if rows_path.exists():
        done_ids = {json.loads(line)["session"] for line in rows_path.open()}
    t_start = time.time()
    for i, h in enumerate(hours):
        sid = f"{h[0]}T{h[1]:02d}"
        options = {"hour": h, "offset_s": rng.uniform(0, 3600), "pilot_seed": rng.randrange(1 << 30), "teacher": True}
        if sid in done_ids:
            continue
        log, times = first_pass(env, options)
        events = targets(log)
        rows = second_pass(env, options, events, times, args.workers) if events else []
        with rows_path.open("a") as f:
            for r in rows:
                f.write(json.dumps({"session": sid, **r}) + "\n")
            if not rows:
                f.write(json.dumps({"session": sid, "event": None}) + "\n")
        print(f"{i + 1}/{len(hours)} {sid}: {len(log['losses'])} losses of separation "
              f"({sum(x['at_stack'] for x in log['losses'])} at the stacks), {len(log['go_around'])} go-arounds, "
              f"{len(rows)} branches; {(time.time() - t_start) / 60:.0f} min", flush=True)
    rows = [json.loads(line) for line in rows_path.open()]
    rows = [r for r in rows if r.get("event")]
    summary = summarise(rows)
    out.with_suffix(".json").write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    sys.exit(main())
