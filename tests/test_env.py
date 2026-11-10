"""Stage 1a's training world: it runs, and what the AI sees and does is well formed."""

import importlib.util

import numpy as np
import pytest

from tests.test_weather import needs_bluesky, needs_sector_file

needs_gym = pytest.mark.skipif(importlib.util.find_spec("gymnasium") is None, reason="gymnasium not installed")


@needs_gym
@needs_sector_file
@needs_bluesky
def test_speed_env_runs_a_real_window():
    from approach.ai.env import N_FEATURES, N_MAX, SpeedEnv

    env = SpeedEnv(window_s=900, seed=3, hours=[("2024-12-07", 14)])
    obs, info = env.reset(options={"hour": ("2024-12-07", 14), "offset_s": 0})
    assert obs.shape == (N_MAX, N_FEATURES) and np.isfinite(obs).all()
    assert env.slots, "an aircraft should be in the sequence once the first decision comes"
    total, steps, done = 0.0, 0, False
    while not done and steps < 400:
        obs, r, done, _, info = env.step(env.action_space.sample())
        assert np.isfinite(obs).all() and np.isfinite(r)
        total += r
        steps += 1
    assert done, "the window should be finished within 400 decisions"
    assert info["counts"]["landed"] >= 1 and "scores" in info
    # Only aircraft in the sequence are listed, in landing order, and the rest of the slots are empty.
    assert (obs[len(env.slots):] == 0).all()


@needs_gym
def test_speeds_stay_in_the_band():
    from approach.controllers import speed

    assert speed.SPEEDS_KT[0] == 160 and speed.SPEEDS_KT[-1] == 250
    assert speed.in_band(250, (160, 180)) == 180  # entering base faster than its band
    assert speed.in_band(160, (180, 250)) == 180
    assert speed.in_band(214, (180, 250)) == 210  # on a 10 kt value
    assert speed.SPEEDS_KT[speed.nearest_choice(222)] == 220
    assert speed.band("cleared", 6.0) is None  # inside 7 NM the pilot flies the approach speeds


needs_sb3 = pytest.mark.skipif(importlib.util.find_spec("stable_baselines3") is None, reason="stable-baselines3 not installed")


@needs_gym
@needs_sb3
def test_only_controlled_aircraft_count_and_the_anchor_holds(tmp_path):
    """SpeedPolicy ignores choices for places the AI doesn't control; AnchoredPPO starts at zero divergence and
    its saved AIs load like any other."""
    import torch
    from gymnasium import spaces

    from approach.ai.env import FEATURES, N_FEATURES, N_MAX, load_policy
    from approach.ai.policy import POLICY_KWARGS
    from approach.ai.ppo import AnchoredPPO, SpeedPolicy, controlled, divergence
    from approach.controllers import speed

    import gymnasium as gym
    from stable_baselines3.common.vec_env import DummyVecEnv

    class Env(gym.Env):  # just enough of an environment to build the model
        observation_space = spaces.Box(-10, 10, (N_MAX, N_FEATURES), np.float32)
        action_space = spaces.MultiDiscrete([len(speed.SPEEDS_KT)] * N_MAX)

        def reset(self, seed=None, options=None):
            return np.zeros((N_MAX, N_FEATURES), np.float32), {}

        def step(self, action):
            return np.zeros((N_MAX, N_FEATURES), np.float32), 0.0, True, False, {}

    model = AnchoredPPO(SpeedPolicy, DummyVecEnv([Env]), policy_kwargs=POLICY_KWARGS, n_steps=64, batch_size=32,
                        device="cpu")
    model.set_anchor(model.policy)
    obs = torch.zeros((1, N_MAX, N_FEATURES))
    obs[0, :3, FEATURES.index("present")] = 1.0
    obs[0, :2, FEATURES.index("controllable")] = 1.0  # places 0 and 1 controlled, place 2 holding
    a = torch.zeros((1, N_MAX), dtype=torch.long)
    b = a.clone()
    b[0, 2:] = 5  # different choices only where they do nothing
    _, lp_a, ent = model.policy.evaluate_actions(obs, a)
    _, lp_b, _ = model.policy.evaluate_actions(obs, b)
    assert torch.allclose(lp_a, lp_b)
    assert float(ent.detach()) <= 2 * np.log(len(speed.SPEEDS_KT)) + 1e-5  # two places' worth of uncertainty at most
    d = model.policy.get_distribution(obs)
    assert float(divergence(d, model.anchor.get_distribution(obs), controlled(obs)).detach()) == pytest.approx(0.0, abs=1e-6)
    model.learn(64)
    model.save(tmp_path / "m.zip")
    act = load_policy(str(tmp_path / "m.zip"))(obs[0].numpy())
    assert act.shape == (N_MAX,)


@needs_gym
@needs_bluesky
@needs_sector_file
def test_stage_1b_turns_aircraft_onto_base_when_told():
    """Stage 1b: the AI's 'turn now' turns an aircraft on the downwind onto base; without it, it flies on."""
    from approach.ai.env import FEATURES_1B, N_MAX, SpeedEnv

    env = SpeedEnv(window_s=1800, seed=3, hours=[("2026-05-30", 12)], base_turns=True)
    obs, _ = env.reset(options={"hour": ("2026-05-30", 12), "offset_s": 0, "pilot_seed": 1})
    assert obs.shape == (N_MAX, len(FEATURES_1B)) and len(env.action_space.nvec) == 2 * N_MAX
    ct = FEATURES_1B.index("can_turn")
    for _ in range(400):
        chance = [i for i in range(len(env.slots)) if obs[i, ct] > 0.5]
        if chance:
            break
        obs, _, done, _, _ = env.step(np.full(2 * N_MAX, 0, dtype=np.int64) + np.r_[[6] * N_MAX, [0] * N_MAX])
        assert not done
    assert chance, "no aircraft reached the point where it could turn base"
    cs = env.slots[chance[0]]
    act = np.r_[[6] * N_MAX, [0] * N_MAX].astype(np.int64)
    act[N_MAX + chance[0]] = 1
    env.step(act)
    env.step(np.r_[[6] * N_MAX, [0] * N_MAX].astype(np.int64))
    assert env.controller.plans[cs].stage != "downwind"
    assert env.counts["base_turns"] == 1
