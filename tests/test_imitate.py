"""The network that copies the look-ahead: its moves, which can be made when, and its choice."""

import importlib.util
from types import SimpleNamespace

import numpy as np
import pytest

from approach.airports.sectorfile import DEFAULT_SECTOR_DIR

pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("bluesky") is None or importlib.util.find_spec("stable_baselines3") is None
    or not (DEFAULT_SECTOR_DIR / "Navaids").is_dir(),
    reason="needs bluesky-simulator, stable-baselines3 and the sector file",
)


@pytest.fixture(scope="module")
def airport():
    from approach.airports import egkk

    return egkk.load()


def test_every_action_is_one_look_ahead_move_and_back():
    from approach.ai import imitate as im

    rows = ([f"S{i}" for i in range(im.N_MAX)], [f"K{j}" for j in range(im.N_STACK)])
    for i in range(im.N_ACTIONS):
        assert im.index_of(im.move_of(i, rows), rows) == i
    assert im.move_of(0, rows) is None and im.index_of(None, rows) == 0
    assert im.index_of(("hold_level", "K3"), rows) == im.index_of(("hold_level", "K3", 0), rows)
    assert im.index_of(("extend", "nobody", 1.0), rows) is None  # an aircraft without a row
    assert im.move_of(1, ([], [])) is None  # an empty row does nothing


def test_moves_that_cant_be_made_now_are_left_out(airport):
    from approach.ai import imitate as im
    from approach.ai.lookahead import MIN_KEEP_S, LookAheadController
    from approach.ai.vector import FEATURES_2, N_MAX

    ctl = LookAheadController(airport, "26L")
    ctl.rules.plans = {
        "A": SimpleNamespace(cs="A", stage="downwind", d_join=ctl.rules.max_join(-1.0) - 1.5, side=-1.0),
        "B": SimpleNamespace(cs="B", stage="inbound"),
    }
    views = [SimpleNamespace(cs="A", alt_ft=4000.0, target_alt=4000.0),
             SimpleNamespace(cs="B", alt_ft=7000.0, target_alt=7000.0)]
    env = SimpleNamespace(vector=ctl, session=SimpleNamespace(world=SimpleNamespace(views=lambda: views)))
    obs = np.zeros((N_MAX, len(FEATURES_2)), dtype=np.float32)
    obs[0, FEATURES_2.index("can_heading")] = 1.0  # A's heading can be adjusted, its level and speed can't
    rows = (["A"], ["B"])
    ctl.offsets["A"] = (2, 0, 0)  # 5 degrees left in force
    made = {("A", 0): 100.0}

    def ok(move, t=150.0):
        return bool(im.valid_moves(env, obs, rows, made, t)[im.index_of(move, rows)])

    assert ok(None)
    assert ok(("set", "A", 0, 4)) and not ok(("set", "A", 0, 3))  # 5 degrees right, not the change in force again
    assert not ok(("set", "A", 1, 2)) and not ok(("set_planned", "A", 2, 1))  # no level or speed adjustments now
    assert not ok(("set_planned", "A", 0, 1)) and ok(("set_planned", "A", 0, 1), t=100.0 + MIN_KEEP_S)
    assert ok(("extend", "A", 1.0)) and not ok(("extend", "A", 2.0))  # 1.5 NM left before the RMA
    assert ok(("hold_level", "B", 0)) and ok(("hold_level", "B", 1)) and not ok(("hold_level", "B", -1))
    assert not ok(("release", "B"))  # nothing to take back
    ctl.held_level["B"] = 7000.0
    made[("B", "stack")] = 150.0
    assert not ok(("hold_level", "B", 0)) and not ok(("release", "B"))
    assert ok(("release", "B"), t=150.0 + MIN_KEEP_S)


def test_the_copy_only_chooses_moves_that_can_be_made():
    import torch

    from approach.ai import imitate as im

    policy = im.make_policy()
    x = torch.zeros(1, im.N_ROWS, len(im.COPY_FEATURES))
    x[0, :2, 0] = 1.0  # two aircraft in the sequence
    mask = torch.zeros(1, im.N_ACTIONS, dtype=torch.bool)
    mask[0, [0, 7, 20]] = True
    p = im.masked_logits(policy, x, mask).exp()[0]
    assert abs(float(p[[0, 7, 20]].sum()) - 1.0) < 1e-5
    driver = im.CopyDriver(policy)
    assert im.index_of(driver.pick(x[0].numpy(), mask[0].numpy(), (["S0", "S1"], [])), (["S0", "S1"], [])) in (0, 7, 20)
