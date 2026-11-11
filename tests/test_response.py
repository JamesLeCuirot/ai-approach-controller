"""Stage 2's first step: pilots who fly like real arrivals, and the model of how aircraft respond."""

import importlib.util
import math

import numpy as np
import pytest

from approach.airports.sectorfile import DEFAULT_SECTOR_DIR
from approach.instructions import Instruction

pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("bluesky") is None or not (DEFAULT_SECTOR_DIR / "Navaids").is_dir(),
    reason="needs bluesky-simulator and the sector file",
)


@pytest.fixture(scope="module")
def airport():
    from approach.airports import egkk

    return egkk.load()


def _one(world, airport, x=-20.0, y=-15.0, hdg=90.0, alt=7000, spd=220, cs="T1"):
    ll = airport.grid.to_latlon(x, y)
    world.spawn(cs, "A320", ll.lat, ll.lon, hdg, alt, spd)
    world.step(10)


def test_measured_pilots_descend_and_slow_down_like_real_arrivals(airport):
    from approach.sim.world import World

    world = World(airport, "26L", seed=5, pilots="measured")
    _one(world, airport)
    p = world.pilots["T1"]
    assert 400 <= p.descent_fpm <= 1800 and 15 <= p.bank_deg <= 26
    world.instruct(Instruction("T1", "altitude", 4000))
    world.instruct(Instruction("T1", "speed", 190))
    vs, cas = [], []
    for _ in range(60):
        world.step(1)
        v = world.views()[0]
        vs.append(v.vs_fpm)
        cas.append(v.cas_kt)
    descending = [x for x in vs if x < -100]
    assert abs(np.median(descending) + p.descent_fpm) < 120  # its own rate, not BlueSky's 1,500 ft/min
    slowed = 220 - cas[-1]
    assert slowed < 60 * 1.1 and slowed > 60 * 0.1  # under about 1 kt a second, not 2-3


def test_bluesky_pilots_are_unchanged(airport):
    from approach.sim.world import World

    world = World(airport, "26L", seed=5)
    _one(world, airport)
    world.instruct(Instruction("T1", "altitude", 4000))
    rates = []
    for _ in range(40):
        world.step(1)
        rates.append(world.views()[0].vs_fpm)
    assert min(rates) == pytest.approx(-1500, abs=5)


def test_small_turns_use_less_bank(airport):
    from approach.sim.world import World

    def peak_rate(size):
        world = World(airport, "26L", seed=7, pilots="measured")
        _one(world, airport)
        world.instruct(Instruction("T1", "heading", 90 + size))
        hd = []
        for _ in range(size + 30):
            world.step(1)
            hd.append(world.views()[0].hdg)
        return max(np.diff(np.unwrap(np.radians(hd)))) * 180 / math.pi

    assert peak_rate(10) < 0.75 * peak_rate(90)


def test_model_follows_the_simulator_through_a_turn(airport):
    from approach.response import ResponseModel, median_pilot, state_from_views
    from approach.sim.world import World

    world = World(airport, "26L", seed=9, pilots="measured")
    _one(world, airport)
    p = world.pilots["T1"]
    state = state_from_views(world.views())
    world.instruct(Instruction("T1", "heading", 180))
    truth = []
    for _ in range(24):
        world.step(5)
        v = world.views()[0]
        truth.append((v.x, v.y))
    pilot = median_pilot(airport.icao)
    pilot.bank_deg[:] = p.bank_deg  # this pilot's own bank and speeds
    pred = ResponseModel(airport, "26L").predict(state, pilot, (0, "heading", 180.0))[0, 0]
    err = math.hypot(pred[-1, 0] - truth[-1][0], pred[-1, 1] - truth[-1][1])
    assert err < 0.25  # NM after 2 minutes (the old look-ahead is typically 0.3 NM out)


def test_crps_of_a_single_guess_is_its_error():
    from approach.response import crps

    assert crps(np.array([[2.0]]), np.array([5.0]))[0] == pytest.approx(3.0)
    draws = np.array([[1.0], [3.0]])
    # E|X - 2| = 1, half of E|X - X'| = 0.5
    assert crps(draws, np.array([2.0]))[0] == pytest.approx(0.5)


def test_safety_check_with_the_model_blocks_a_turn_into_traffic(airport):
    from approach.controllers.safety import SafetyCheck
    from approach.sim.world import World

    world = World(airport, "26L", seed=3, pilots="measured")
    for cs, y in (("A", -10.0), ("B", -5.0)):
        ll = airport.grid.to_latlon(-20.0, y)
        world.spawn(cs, "A320", ll.lat, ll.lon, 90.0, 6000, 220)
    world.step(5)
    check = SafetyCheck(airport, "26L", lookahead="model")
    assert not check.review(Instruction("A", "heading", 30), world.views())[0]
    assert check.review(Instruction("A", "heading", 150), world.views())[0]


def test_stage2_world_as_planned_adds_no_instructions_and_choices_take_effect():
    import torch as th

    from approach.ai.ppo import controlled
    from approach.ai.vector import N_MAX, VectorEnv, as_planned

    env = VectorEnv(seed=1, cached_winds_only=True)
    env.reset(seed=3)
    for _ in range(60):
        obs, _, done, _, _ = env.step(as_planned())
        if done:
            break
    assert env.counts["instructions"] == 0  # "as planned" is the planner's own clearances
    mask = controlled(th.tensor(obs[None]), 3 * N_MAX)[0]
    assert mask.shape[0] == 3 * N_MAX
    action = as_planned()
    action[:N_MAX] = 4  # 10 degrees right of the planned heading wherever the AI can turn
    before = env.counts["instructions"]
    for _ in range(6):
        env.step(action)
    if mask[:N_MAX].sum() > 0:
        assert env.counts["instructions"] > before
