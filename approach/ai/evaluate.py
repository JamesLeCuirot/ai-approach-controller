"""Test a trained AI against the speed-only rules controller on traffic it never practised on.

    python -m approach.ai.evaluate runs/stage1a-04/model_final.zip

Sessions are drawn from the test days and the rolling recent set (never the training days):
a real hour, a start minute and a pilot seed each. Both controllers fly exactly the same
sessions, in the same winds with the same pilots, so the only difference is who chose the
speeds. Stage 1a's gate: across the sessions, gaps closer to the target than the speed-only
controller, with no separation losses, no wake infringements and no more speed instructions.

Separation losses swing a lot from one session to the next, so the summary also gives, for the
AI minus the speed-only controller, each total difference with its 95% range: a difference
inside the range could be chance. 120 sessions is the default (40 left too much to chance).

Writes <model>.eval.csv (one row per session and controller) and <model>.eval.json (totals and
the gate), next to the model.

Stage 2 (--vector): the AI adjusts the planner's headings, levels and speeds (approach.ai.vector), with pilots who
fly like real arrivals and the model look-ahead; the competitor is the planner's own proposals ("rules_vector",
"as planned" everywhere). The real controllers' results on the same hours are added as "real", scored with the
same code, as the target.

The speed-only controller's results on a set of sessions don't change until its code does, so
they are saved in runs/competitor/ and reused: each later test only flies the AI, which halves
the time. The saved results are keyed on the sessions and on the code that flies them.
"""

from __future__ import annotations

import argparse
import csv
import json
import random
from datetime import date
from multiprocessing import Pool
from pathlib import Path

import numpy as np

from approach.data.arrivals import REAL_DIR


def test_hours(splits: tuple[str, ...] = ("test", "recent"), min_arrivals: int = 3) -> list[tuple[str, int]]:
    """Every hour with traffic on the test and recent days, from the arrivals themselves."""
    from approach.data.arrivals import load_day

    days = [r["day"] for r in csv.DictReader((REAL_DIR / "egkk" / "days.csv").open()) if r["split"] in splits]
    out = []
    for d in days:
        if not (REAL_DIR / "egkk" / "arrivals" / f"{d}.json.gz").is_file():
            continue
        counts = [0] * 24
        for a in load_day(date.fromisoformat(d), "EGKK")["arrivals"]:
            from datetime import datetime, timezone

            counts[datetime.fromtimestamp(a["handover"]["t"], tz=timezone.utc).hour] += 1
        out += [(d, h) for h in range(24) if counts[h] >= min_arrivals]
    return out


COMPETITOR_DIR = REAL_DIR.parents[1] / "runs" / "competitor"
CODE_FILES = ("approach/controllers", "approach/sim", "approach/ai/env.py", "approach/wake.py", "approach/scoring.py",
              "approach/data/real_sessions.py", "approach/airports", "approach/ai/vector.py", "approach/response.py")


def _fields(rows: list[dict]) -> list[str]:
    """Every column in any row, in the order first seen (the real controllers' rows have fewer)."""
    out: dict[str, None] = {}
    for r in rows:
        out.update(dict.fromkeys(r))
    return list(out)


def code_hash() -> str:
    """A fingerprint of the code that flies and scores the competitor's sessions."""
    import hashlib

    root = REAL_DIR.parents[1]
    h = hashlib.sha256()
    for name in CODE_FILES:
        path = root / name
        for f in sorted(path.rglob("*.py")) if path.is_dir() else [path]:
            h.update(f.read_bytes())
    return h.hexdigest()[:12]


def _run(args: tuple) -> list[dict]:
    model_path, sessions, with_competitor, base_turns, vector = (*args, False, False)[:5]
    from approach.ai.env import SpeedEnv, load_policy
    from approach.ai.train import episode_row

    policy = load_policy(model_path) if model_path else None
    if vector:
        from approach.ai.vector import VectorEnv

        env, competitor = VectorEnv(hours=[s["hour"] for s in sessions]), "rules_vector"
    else:
        env, competitor = SpeedEnv(hours=[s["hour"] for s in sessions], base_turns=base_turns), "rules_speed"
    rows = []
    for s in sessions:
        try:
            pair = []
            for who in (competitor, "ai"):
                if (who == "ai" and policy is None) or (who == competitor and not with_competitor):
                    continue
                options = {"hour": s["hour"], "offset_s": s["offset_s"], "pilot_seed": s["pilot_seed"],
                           "teacher": who == competitor}
                obs, _ = env.reset(options=options)
                done, total = False, 0.0
                while not done:
                    act = env.teacher_action() if who == competitor else policy(obs)
                    obs, r, done, _, info = env.step(act)
                    total += r
                pair.append({"session": s["id"], "controller": who, "hour": f"{s['hour'][0]}T{s['hour'][1]:02d}",
                             "offset_min": round(s["offset_s"] / 60), **episode_row(info, total)})
            rows += pair  # both controllers or neither, so the comparison stays like for like
        except Exception as e:  # noqa: BLE001 - one failed session must not stop the test
            print(f"session {s['id']} {s['hour']} skipped: {e}", flush=True)
    return rows


def paired_differences(rows: list[dict], a: str = "ai", b: str = "rules_speed") -> dict:
    """``a`` minus ``b`` (by default the AI minus the speed-only controller), session by session: each total
    difference and its 95% range."""
    by = {(int(r["session"]), r["controller"]): r for r in rows}
    sessions = sorted({k for k, who in by if who == a and (k, b) in by})
    out = {}
    for key in ("separation_losses", "losses_at_stack", "wake_infringements", "go_arounds", "gaps_inside_minimum",
                "landed"):
        d = np.array([int(by[k, a].get(key) or 0) - int(by[k, b].get(key) or 0) for k in sessions], dtype=float)
        if len(d) < 2:
            continue
        half = 1.96 * d.std(ddof=1) * np.sqrt(len(d))
        out[key] = {"difference": int(d.sum()), "range_95": round(float(half), 1),
                    "beyond_chance": bool(abs(d.sum()) > half)}
    return out


def real_rows(sessions: list[dict]) -> list[dict]:
    """The real controllers on the same hours (from each session's start minute), scored with the same code."""
    from datetime import datetime, timezone

    from approach.airports import egkk
    from approach.data.arrivals import load_day
    from approach.data.real_sessions import real_session

    airport, days, rows = egkk.load(), {}, []
    for s in sessions:
        d, h = s["hour"]
        if d not in days:
            days[d] = load_day(date.fromisoformat(d), "EGKK")
        start = datetime.fromisoformat(d).replace(tzinfo=timezone.utc).timestamp() + h * 3600 + s["offset_s"]
        log = real_session(days[d], airport, start, start + 3600)
        sc = log.scores
        rows.append({"session": s["id"], "controller": "real", "hour": f"{d}T{h:02d}",
                     "offset_min": round(s["offset_s"] / 60), "landed": sc.get("landed", 0),
                     "separation_losses": sc.get("separation_losses", 0), "losses_at_stack": "",
                     "wake_infringements": sc.get("wake_violations", 0),
                     "go_arounds": sum(1 for e in log.events if e.get("kind") == "go_around"),
                     "speed_instructions": 0, "blocked": 0, "decisions": 0, "gaps": 0, "mean_gap_error_nm": "",
                     "gaps_inside_minimum": 0, "scored_separation_losses": sc.get("separation_losses"),
                     "minutes_per_arrival": sc.get("minutes_per_arrival", ""), "reward": 0.0})
    return rows


def summarise(rows: list[dict]) -> dict:
    out = {}
    for who in sorted({r["controller"] for r in rows}):
        g = [r for r in rows if r["controller"] == who]
        landed = sum(int(r["landed"]) for r in g)
        errs = [float(r["mean_gap_error_nm"]) * int(r["gaps"]) for r in g if r["mean_gap_error_nm"] != ""]
        gaps = sum(int(r["gaps"]) for r in g)
        out[who] = {
            "sessions": len(g), "landed": landed,
            "mean_gap_error_nm": round(sum(errs) / gaps, 3) if gaps else None,
            "gaps_inside_minimum": sum(int(r["gaps_inside_minimum"]) for r in g),
            "separation_losses": sum(int(r["separation_losses"]) for r in g),
            "losses_at_stack": sum(int(r.get("losses_at_stack") or 0) for r in g),
            "wake_infringements": sum(int(r["wake_infringements"]) for r in g),
            "go_arounds": sum(int(r["go_arounds"]) for r in g),
            "speed_instructions_per_arrival": round(sum(int(r["speed_instructions"]) for r in g) / max(landed, 1), 2),
            "mean_reward": round(float(np.mean([float(r["reward"]) for r in g])), 2),
        }
        minutes = [(float(r["minutes_per_arrival"]), int(r["landed"])) for r in g if r.get("minutes_per_arrival")]
        if minutes:
            out[who]["minutes_per_arrival"] = round(sum(m * n for m, n in minutes) / max(sum(n for _, n in minutes), 1), 1)
    if "ai" in out and "rules_vector" in out:
        out["ai_minus_rules_vector"] = paired_differences(rows, "ai", "rules_vector")
    if "rules_vector" in out and "real" in out:
        out["rules_vector_minus_real"] = paired_differences(rows, "rules_vector", "real")
    if "ai" in out and "rules_speed" in out:
        out["ai_minus_rules_speed"] = paired_differences(rows)
        a, b = out["ai"], out["rules_speed"]
        out["gate"] = {
            "closer_gaps": a["mean_gap_error_nm"] < b["mean_gap_error_nm"],
            "no_separation_losses": a["separation_losses"] == 0,
            "no_wake_infringements": a["wake_infringements"] == 0,
            "no_more_instructions": a["speed_instructions_per_arrival"] <= b["speed_instructions_per_arrival"],
        }
        out["gate"]["passed"] = all(out["gate"].values())
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("model", nargs="?", default="", help="a saved AI; leave out to test only the rules controller")
    ap.add_argument("--sessions", type=int, default=120)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--seed", type=int, default=2026)
    ap.add_argument("--out", default="")
    ap.add_argument("--base-turns", action="store_true",
                    help="stage 1b: the AI also turns aircraft onto base; the competitor is the eager base-turn rule")
    ap.add_argument("--vector", action="store_true",
                    help="stage 2: the AI adjusts the planner's headings, levels and speeds; the competitor is the "
                         "planner's own proposals, and the real controllers are added as the target")
    args = ap.parse_args()
    rng = random.Random(args.seed)
    hours = test_hours()
    sessions = [{"id": k, "hour": rng.choice(hours), "offset_s": rng.uniform(0, 3600), "pilot_seed": rng.randrange(1 << 30)}
                for k in range(args.sessions)]
    # Download the winds first, so the test itself only flies.
    from approach.data.gfs import _prefetch_one

    needed = sorted({f"{d}T{h:02d}" for d, h in (s["hour"] for s in sessions)})
    with Pool(3) as pool:
        for err in pool.imap_unordered(_prefetch_one, needed):
            if err:
                print("winds:", err, flush=True)
    prefix = "2_" if args.vector else ("1b_" if args.base_turns else "")
    cache = COMPETITOR_DIR / f"{prefix}seed{args.seed}_n{args.sessions}_{code_hash()}.csv"
    saved = list(csv.DictReader(cache.open())) if cache.is_file() else []
    if saved:
        print(f"Reusing the speed-only controller's results from {cache.name}", flush=True)
    chunks = [(args.model, sessions[k::args.workers], not saved, args.base_turns, args.vector)
              for k in range(args.workers)]
    with Pool(args.workers) as pool:
        rows = [r for part in pool.map(_run, chunks) for r in part]
    if args.vector and not saved:
        rows += real_rows(sessions)  # saved with the competitor's results, so computed once
    if saved:
        flown = {int(r["session"]) for r in rows}
        rows += [r for r in saved if int(r["session"]) in flown]  # like for like: only sessions the AI flew
    else:
        cache.parent.mkdir(parents=True, exist_ok=True)
        competitor = [r for r in rows if r["controller"] in ("rules_speed", "rules_vector", "real")]
        with cache.open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=_fields(competitor))
            w.writeheader()
            w.writerows(competitor)
    rows.sort(key=lambda r: (int(r["session"]), r["controller"]))
    base = Path(args.out or (args.model or ("runs/rules_vector" if args.vector else "runs/rules_speed"))).with_suffix("")
    with Path(f"{base}.eval.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=_fields(rows))
        w.writeheader()
        w.writerows(rows)
    summary = {"model": args.model, "sessions": args.sessions, "test_hours": len(hours), "seed": args.seed,
               **summarise(rows)}
    Path(f"{base}.eval.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
