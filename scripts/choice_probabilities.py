"""How likely the stage 2 AI is to pick "as planned" for each heading, level and speed choice it can make, at each
saved AI of a run. Observations come from as-planned sessions on six test hours. For an AI whose choices hold
(stage2-03 on), choice 0 is "keep what's in force", which with nothing in force is as planned, and the
alternatives are choices 2 on ("as planned", choice 1, is then the same as keeping).

Usage: PYTHONPATH=. python scripts/choice_probabilities.py [--run stage2-02] headstart 100k 300k  (names of runs/RUN/model_*.zip;
RUN defaults to stage2-01)"""
import sys
import numpy as np
import torch as th
from stable_baselines3 import PPO

from approach.ai.ppo import controlled
from approach.ai.vector import HOLD_CHOICES, N_MAX, VectorEnv
from approach.ai.evaluate import test_hours

hours = test_hours()[:: max(1, len(test_hours()) // 6)][:6]
env = VectorEnv(hours=hours, cached_winds_only=True)
obs_list = []
for i, h in enumerate(hours):
    obs, _ = env.reset(options={"hour": h, "offset_s": 0, "pilot_seed": i, "teacher": True})
    done = False
    while not done:
        obs_list.append(obs)
        obs, _, done, _, _ = env.step(env.teacher_action())
obs = th.tensor(np.array(obs_list), dtype=th.float32)
print(f"{len(obs_list)} decisions from {len(hours)} test hours")
names = ["heading", "level", "speed"]
args = sys.argv[1:]
run = "stage2-01"
if args[:1] == ["--run"]:
    run, args = args[1], args[2:]
for tag in args:
    m = PPO.load(f"runs/{run}/model_{tag}.zip", device="cpu")
    first_alt = 2 if int(m.action_space.nvec[0]) == HOLD_CHOICES[0] else 1
    with th.no_grad():
        dist = m.policy.get_distribution(obs)
    probs = [d.probs.numpy() for d in dist.distribution]  # 48 x (B, k)
    mask = controlled(obs, 3 * N_MAX).numpy().astype(bool)
    out = [tag]
    for b, name in enumerate(names):
        p0, alt = [], []
        for j in range(N_MAX):
            col = b * N_MAX + j
            sel = mask[:, col]
            if sel.any():
                p = probs[col][sel]
                p0.append(p[:, 0]); alt.append(p[:, first_alt:].max(axis=1))
        p0, alt = np.concatenate(p0), np.concatenate(alt)
        out.append(f"{name}: {'keep' if first_alt == 2 else 'as planned'} median {np.median(p0):.3f} min {p0.min():.3f}; best other median {np.median(alt):.3f} "
                   f"max {alt.max():.3f}; other>0.1 {np.mean(alt > 0.1):.1%}, >0.3 {np.mean(alt > 0.3):.1%}, "
                   f"other more likely than as planned {np.mean(alt > p0):.1%}, n={len(p0)}")
    print("\n  ".join(out))
