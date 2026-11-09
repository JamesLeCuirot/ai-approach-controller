"""How fast does stage 1a's training world run? Decisions per second, one process and several.

    python scripts/measure_sim_speed.py              (episodes of random speed choices)
    python scripts/measure_sim_speed.py --workers 4

Prints decisions per second, simulated hours per hour, and how long a million decisions
(stage 1a's first training run) would take.
"""

from __future__ import annotations

import argparse
import sys
import time
from multiprocessing import Pool
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def run_episodes(args: tuple[int, int, list | None]) -> tuple[int, float, float]:
    """Run episodes with random actions; return (decisions, simulated seconds, wall seconds)."""
    seed, episodes, hours = args
    from approach.ai.env import SpeedEnv

    env = SpeedEnv(seed=seed, hours=hours)
    decisions, sim_s, wall = 0, 0.0, 0.0
    for _ in range(episodes):
        env.reset()
        t0 = time.time()
        done = False
        while not done:
            _, _, done, _, _ = env.step(env.action_space.sample())
            decisions += 1
        wall += time.time() - t0
        sim_s += env.session.t
    return decisions, sim_s, wall


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--episodes", type=int, default=3, help="episodes per worker")
    ap.add_argument("--workers", type=int, default=1)
    ap.add_argument("--cached-winds-only", action="store_true", help="only hours whose winds are already downloaded")
    args = ap.parse_args()
    hours = None
    if args.cached_winds_only:
        from approach.ai.env import training_hours
        from approach.data.gfs import CACHE_DIR

        hours = [(d, h) for d, h in training_hours()
                 if (CACHE_DIR / f"egkk_{d.replace('-', '')}{h:02d}.json").is_file()]
        print(f"{len(hours)} training hours with winds downloaded")
    t0 = time.time()
    jobs = [(100 + k, args.episodes, hours) for k in range(args.workers)]
    if args.workers == 1:
        results = [run_episodes(jobs[0])]
    else:
        with Pool(args.workers) as pool:
            results = pool.map(run_episodes, jobs)
    wall = time.time() - t0
    decisions = sum(r[0] for r in results)
    sim_s = sum(r[1] for r in results)
    per_s = decisions / wall
    print(f"{args.workers} worker(s), {args.workers * args.episodes} episodes: {decisions} decisions, "
          f"{sim_s / 3600:.1f} simulated hours in {wall:.0f} s")
    print(f"  {per_s:.0f} decisions per second, {sim_s / wall:.0f}x real time overall")
    print(f"  a million decisions: about {1e6 / per_s / 3600:.1f} hours (simulation only; learning adds some)")


if __name__ == "__main__":
    main()
