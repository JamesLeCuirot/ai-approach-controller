"""Fly versions of the rules controller on the test sessions and compare each with the speed-only rules controller.

    python scripts/compare_controllers.py fixed_turn stretch --workers 4

Each version flies the same sessions as approach.ai.evaluate (seed 2026, 120 sessions: real hours from the test
days and the most recent fortnight, the same start minutes, winds and pilots), with no AI: every speed the
controller doesn't give itself is left alone. Results go to runs/baselines/<version>.eval.csv and .eval.json, with
each total given as the version minus the speed-only rules controller and its 95% range.
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

VERSIONS = {
    # Stage 1a's competitor, timed with the wind: every arrival turns base at the same point (an 11 NM final).
    "fixed_turn": {"fixed_paths": True, "speeds": "slot", "wind_aware": True},
    # The base turn planned to make each landing slot (the downwind stretched or cut short), timed with the wind.
    "stretch": {"fixed_paths": False, "speeds": "slot", "wind_aware": True},
    # ... with stage 0's final director (160 or 180 kt on final to keep the gap) instead of the standard speeds.
    "stretch_final_director": {"fixed_paths": False, "speeds": "rules", "wind_aware": True},
    # Stage 0's rules controller as it was: base turn planned to the slot, final director, timed for still air.
    "stage0_still_air": {"fixed_paths": False, "speeds": "rules", "wind_aware": False},
}


def _fly(args: tuple) -> list[dict]:
    name, sessions = args
    import numpy as np

    from approach.ai.env import FEATURES, N_MAX, SpeedEnv
    from approach.ai.train import episode_row
    from approach.controllers import speed

    ak = FEATURES.index("assigned_kt")
    env = SpeedEnv(hours=[s["hour"] for s in sessions], planner=VERSIONS[name])
    rows = []
    for s in sessions:
        try:
            obs, _ = env.reset(options={"hour": s["hour"], "offset_s": s["offset_s"], "pilot_seed": s["pilot_seed"]})
            done, total = False, 0.0
            while not done:
                leave_alone = np.array([speed.nearest_choice(float(obs[i, ak]) * 250.0) for i in range(N_MAX)])
                obs, r, done, _, info = env.step(leave_alone)
                total += r
            rows.append({"session": s["id"], "controller": "ai", "hour": f"{s['hour'][0]}T{s['hour'][1]:02d}",
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
    args = ap.parse_args()
    rng = random.Random(args.seed)
    hours = evaluate.test_hours()
    sessions = [{"id": k, "hour": rng.choice(hours), "offset_s": rng.uniform(0, 3600), "pilot_seed": rng.randrange(1 << 30)}
                for k in range(args.sessions)]
    competitor = Path(args.competitor) if args.competitor else \
        evaluate.COMPETITOR_DIR / f"seed{args.seed}_n{args.sessions}_{evaluate.code_hash()}.csv"
    if not competitor.is_file():
        raise SystemExit(f"No saved results for the speed-only controller at {competitor}: run approach.ai.evaluate first")
    saved = list(csv.DictReader(competitor.open()))
    out_dir = ROOT / "runs" / "baselines"
    out_dir.mkdir(parents=True, exist_ok=True)
    for name in args.versions:
        with Pool(args.workers) as pool:
            rows = [r for part in pool.map(_fly, [(name, sessions[k::args.workers]) for k in range(args.workers)])
                    for r in part]
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


if __name__ == "__main__":
    main()
