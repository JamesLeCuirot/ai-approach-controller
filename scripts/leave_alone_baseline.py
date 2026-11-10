"""Run from the repository root: python scripts/leave_alone_baseline.py [workers]

How much does speed control matter here? Fly the 120 test sessions with every speed left alone (only the band
edges applied, as for any controller) and compare with the speed-only rules controller on the same sessions."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import numpy as np  # noqa: E402

import approach.ai.env as E  # noqa: E402
from approach.controllers import speed  # noqa: E402

AK = E.FEATURES.index("assigned_kt")
_orig = E.load_policy


def load_policy(path):
    if path == "LEAVE_ALONE":
        return lambda obs: np.array([speed.nearest_choice(float(obs[i, AK]) * 250.0) for i in range(E.N_MAX)])
    return _orig(path)


E.load_policy = load_policy
from approach.ai import evaluate  # noqa: E402

workers = sys.argv[1] if len(sys.argv) > 1 else "4"
sys.argv = ["evaluate", "LEAVE_ALONE", "--workers", workers, "--out", "runs/baselines/leave_alone"]
evaluate.main()
