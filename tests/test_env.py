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
def test_speed_steps_stay_in_the_band():
    from approach.controllers import speed

    assert speed.apply_step(220, 20, (180, 250)) == 240
    assert speed.apply_step(250, 20, (180, 250)) == 250
    assert speed.apply_step(280, -20, (180, 250)) == 250  # above the band: straight to its edge
    assert speed.apply_step(220, 0, (160, 180)) == 180  # entering base faster than its band
    assert speed.band("cleared", 6.0) is None  # inside 7 NM the pilot flies the approach speeds
