"""Fly versions of the rules controller on the test sessions and compare each with the speed-only rules controller.

    python scripts/compare_controllers.py fixed_turn stretch --workers 4

Each version flies the same sessions as approach.ai.evaluate (seed 2026, 120 sessions: real hours from the test
days and the most recent fortnight, the same start minutes, winds and pilots), with no AI: every speed the
controller doesn't give itself is left alone. Results go to runs/baselines/<version>.eval.csv and .eval.json, with
each total given as the version minus the speed-only rules controller and its 95% range.

With --pilots measured, the pilots turn, descend and slow down like real Gatwick arrivals (stage 2), and the
results go to runs/baselines/<version>_measured.eval.*, each total given as the version with measured pilots minus
the same version with BlueSky's pilots (its saved results), session by session.
"""

from __future__ import annotations

import argparse
import csv
import json
import random
import sys
from multiprocessing import Pool
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from approach.ai import evaluate  # noqa: E402

from approach.controllers.versions import VERSIONS  # noqa: E402


def _fly(args: tuple) -> list[dict]:
    name, sessions, pilots = args
    import numpy as np

    from approach.ai.env import FEATURES, N_MAX, SpeedEnv
    from approach.ai.train import episode_row
    from approach.controllers import speed

    ak = FEATURES.index("assigned_kt")
    env = SpeedEnv(hours=[s["hour"] for s in sessions], planner=VERSIONS[name], pilots=pilots)
    rows = []
    for s in sessions:
        try:
            obs, _ = env.reset(options={"hour": s["hour"], "offset_s": s["offset_s"], "pilot_seed": s["pilot_seed"]})
            done, total = False, 0.0
            while not done:
                leave_alone = np.array([speed.nearest_choice(float(obs[i, ak]) * 250.0) for i in range(N_MAX)])
                obs, r, done, _, info = env.step(leave_alone)
                total += r
            rows.append({"session": s["id"], "controller": "ai" if pilots == "bluesky" else pilots, "hour": f"{s['hour'][0]}T{s['hour'][1]:02d}",
                         "offset_min": round(s["offset_s"] / 60), **episode_row(info, total)})
        except Exception as e:  # noqa: BLE001 - one failed session must not stop the comparison
            print(f"{name}: session {s['id']} skipped: {e}", flush=True)
    return rows


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("versions", nargs="+", choices=sorted(VERSIONS))
    ap.add_argument("--sessions", type=int, default=120)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--seed", type=int, default=2026)
    ap.add_argument("--competitor", default="", help="the speed-only controller's saved results (runs/competitor/...)")
    ap.add_argument("--pilots", default="bluesky", choices=("bluesky", "measured"))
    args = ap.parse_args()
    rng = random.Random(args.seed)
    hours = evaluate.test_hours()
    sessions = [{"id": k, "hour": rng.choice(hours), "offset_s": rng.uniform(0, 3600), "pilot_seed": rng.randrange(1 << 30)}
                for k in range(args.sessions)]
    out_dir = ROOT / "runs" / "baselines"
    out_dir.mkdir(parents=True, exist_ok=True)
    if args.pilots == "measured":
        for name in args.versions:
            measured(name, sessions, args, out_dir)
        return
    competitor = Path(args.competitor) if args.competitor else \
        evaluate.COMPETITOR_DIR / f"seed{args.seed}_n{args.sessions}_{evaluate.code_hash()}.csv"
    if not competitor.is_file():
        raise SystemExit(f"No saved results for the speed-only controller at {competitor}: run approach.ai.evaluate first")
    saved = list(csv.DictReader(competitor.open()))
    for name in args.versions:
        with Pool(args.workers) as pool:
            rows = [r for part in pool.map(_fly, [(name, sessions[k::args.workers], "bluesky")
                                                  for k in range(args.workers)]) for r in part]
        flown = {int(r["session"]) for r in rows}
        both = sorted(rows + [r for r in saved if int(r["session"]) in flown], key=lambda r: (int(r["session"]), r["controller"]))
        with (out_dir / f"{name}.eval.csv").open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(both[0]))
            w.writeheader()
            w.writerows(both)
        summary = {"version": name, "settings": VERSIONS[name], "sessions": args.sessions, "seed": args.seed,
                   "competitor": competitor.name, **evaluate.summarise(both)}
        summary.pop("gate", None)
        (out_dir / f"{name}.eval.json").write_text(json.dumps(summary, indent=2))
        print(name, json.dumps({k: summary[k] for k in ("ai", "ai_minus_rules_speed")}), flush=True)


def measured(name: str, sessions: list[dict], args, out_dir: Path) -> None:
    """The version with measured pilots, against the same version with BlueSky's pilots on the same sessions."""
    before = out_dir / f"{name}.eval.csv"
    if not before.is_file():
        raise SystemExit(f"No saved results for {name} with BlueSky's pilots at {before}: run without --pilots first")
    saved = [{**r, "controller": "bluesky"} for r in csv.DictReader(before.open()) if r["controller"] == "ai"]
    with Pool(args.workers) as pool:
        rows = [r for part in pool.map(_fly, [(name, sessions[k::args.workers], "measured")
                                              for k in range(args.workers)]) for r in part]
    flown = {int(r["session"]) for r in rows}
    both = sorted(rows + [r for r in saved if int(r["session"]) in flown],
                  key=lambda r: (int(r["session"]), r["controller"]))
    with (out_dir / f"{name}_measured.eval.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(both)
    summary = {"version": name, "settings": VERSIONS[name], "pilots": "measured", "sessions": args.sessions,
               "seed": args.seed, **evaluate.summarise(both),
               "measured_minus_bluesky": evaluate.paired_differences(both, "measured", "bluesky")}
    (out_dir / f"{name}_measured.eval.json").write_text(json.dumps(summary, indent=2))
    print(name, json.dumps({k: summary[k] for k in ("measured", "bluesky", "measured_minus_bluesky")}), flush=True)


if __name__ == "__main__":
    main()
