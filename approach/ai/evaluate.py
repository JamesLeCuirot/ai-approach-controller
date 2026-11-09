"""Test a trained AI against the speed-only rules controller on traffic it never practised on.

    python -m approach.ai.evaluate runs/stage1a-01/model_final.zip --sessions 120

Sessions are drawn from the test days and the rolling recent set (never the training days):
a real hour, a start minute and a pilot seed each. Both controllers fly exactly the same
sessions, in the same winds with the same pilots, so the only difference is who chose the
speeds. Stage 1a's gate: across the sessions, gaps closer to the target than the speed-only
controller, with no separation losses, no wake infringements and no more speed instructions.

Writes <model>.eval.csv (one row per session and controller) and <model>.eval.json (totals and
the gate), next to the model.
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


def _run(args: tuple) -> list[dict]:
    model_path, sessions = args
    from approach.ai.env import SpeedEnv, load_policy
    from approach.ai.train import episode_row

    policy = load_policy(model_path) if model_path else None
    env = SpeedEnv(hours=[s["hour"] for s in sessions])
    rows = []
    for s in sessions:
        try:
            pair = []
            for who in ("rules_speed", "ai"):
                if who == "ai" and policy is None:
                    continue
                options = {"hour": s["hour"], "offset_s": s["offset_s"], "pilot_seed": s["pilot_seed"],
                           "teacher": who == "rules_speed"}
                obs, _ = env.reset(options=options)
                done, total = False, 0.0
                while not done:
                    act = env.teacher_action() if who == "rules_speed" else policy(obs)
                    obs, r, done, _, info = env.step(act)
                    total += r
                pair.append({"session": s["id"], "controller": who, "hour": f"{s['hour'][0]}T{s['hour'][1]:02d}",
                             "offset_min": round(s["offset_s"] / 60), **episode_row(info, total)})
            rows += pair  # both controllers or neither, so the comparison stays like for like
        except Exception as e:  # noqa: BLE001 - one failed session must not stop the test
            print(f"session {s['id']} {s['hour']} skipped: {e}", flush=True)
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
            "wake_infringements": sum(int(r["wake_infringements"]) for r in g),
            "go_arounds": sum(int(r["go_arounds"]) for r in g),
            "speed_instructions_per_arrival": round(sum(int(r["speed_instructions"]) for r in g) / max(landed, 1), 2),
            "mean_reward": round(float(np.mean([float(r["reward"]) for r in g])), 2),
        }
    if "ai" in out:
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
    chunks = [(args.model, sessions[k::args.workers]) for k in range(args.workers)]
    with Pool(args.workers) as pool:
        rows = [r for part in pool.map(_run, chunks) for r in part]
    rows.sort(key=lambda r: (r["session"], r["controller"]))
    base = Path(args.out or (args.model or "runs/rules_speed") ).with_suffix("")
    with Path(f"{base}.eval.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    summary = {"model": args.model, "sessions": args.sessions, "test_hours": len(hours), "seed": args.seed,
               **summarise(rows)}
    Path(f"{base}.eval.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
