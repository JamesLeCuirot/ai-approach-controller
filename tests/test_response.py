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


def test_losses_out_of_the_ais_reach_can_be_left_out_of_its_reward():
    from types import SimpleNamespace

    from approach.ai.vector import VectorEnv

    def reward(stage, weights):
        env = VectorEnv(seed=1, cached_winds_only=True, reward=weights)
        env.reset(seed=3)
        a = SimpleNamespace(cs="A", x=0.0, y=0.0, alt_ft=5000, loc=False, along=20.0, actype="A320")
        b = SimpleNamespace(cs="B", x=1.0, y=0.0, alt_ft=5200, loc=False, along=21.0, actype="A320")
        env.session.views, env.in_conflict, env.wake_flagged = [a, b], set(), set()
        env.controller.stage_of = lambda cs: stage
        before = dict(env.counts)
        r = env._conflicts()
        return r, env.counts["losses_out_of_reach"] - before["losses_out_of_reach"]

    off = {"separation_loss_out_of_reach": 0.0}
    assert reward("inbound", None) == (-5.0, 1)  # unchanged unless a run says otherwise
    assert reward("inbound", off) == (0.0, 1)  # both in or on their way to a stack: out of the AI's reach
    assert reward("downwind", off) == (-5.0, 0)  # the AI could have adjusted these


def test_held_choices_stay_in_force_until_the_ai_changes_them(airport):
    from types import SimpleNamespace

    from approach.ai.vector import HEADING_OFFSETS, N_MAX, PLANNED, VectorController, as_planned

    ctl = VectorController(airport, "26L", hold=True)
    ctl.rules.plans["A"] = SimpleNamespace(stage="downwind", hdg_cmd=None, spd_cmd=None, alt_cmd=None)
    v = SimpleNamespace(cs="A", loc=False, gs=False, along=20.0)
    right10 = HEADING_OFFSETS.index(10)

    def choose(heading=None, level=None, views=(v,)):
        a = as_planned()  # all "keep"
        if heading is not None:
            a[0] = heading
        if level is not None:
            a[N_MAX] = level
        ctl.set_action(a, ["A"], list(views))
        return ctl.offsets["A"]

    assert choose() == (0, 0, 0)  # keeping with nothing in force is as planned
    assert choose(heading=PLANNED + right10)[0] == right10
    for _ in range(5):
        assert choose()[0] == right10  # kept until the AI changes it
    assert choose(heading=PLANNED)[0] == 0  # back to as planned
    choose(heading=PLANNED + right10, level=PLANNED + 2)
    assert choose() == (right10, 2, 0)
    v.loc = True  # on the localiser: the heading can no longer be adjusted, the level still can
    assert choose() == (0, 2, 0)
    v.loc = False
    choose(heading=PLANNED + right10)
    ctl.ai_said.add(("A", "heading", 100.0))
    ctl.on_event({"kind": "blocked", "instruction": Instruction("A", "heading", 100.0), "t": 0})
    assert ctl.offsets["A"] == (0, 2, 0)  # blocked: back to as planned rather than asking again every 10 s
    assert ctl.ai_blocked == 1


def test_the_head_start_habit_tries_now_and_then_and_goes_back():
    from approach.ai.vector import FEATURES_2, HABIT, HOLD_CHOICES, KEEP, N_MAX, PLANNED, habit_probs

    obs = np.zeros((2, N_MAX, len(FEATURES_2)), dtype=np.float32)
    obs[1, 0, FEATURES_2.index("heading_offset")] = 1.0  # 10 degrees right in force
    obs[1, 0, FEATURES_2.index("level_offset")] = -1.0  # 1,000 ft below in force
    heading, level, speed = habit_probs(obs)
    assert [p.shape[-1] for p in (heading, level, speed)] == list(HOLD_CHOICES)
    for p in (heading, level, speed):
        assert np.allclose(p.sum(axis=-1), 1.0)
    assert heading[0, 0, KEEP] == pytest.approx(1 - HABIT["try"])
    assert heading[0, 0, PLANNED] == 0.0  # "as planned" with nothing in force is keeping
    assert heading[1, 0, KEEP] == pytest.approx(1 - HABIT["back"] - HABIT["switch"])
    assert heading[1, 0, PLANNED] == pytest.approx(HABIT["back"])
    assert heading[1, 0, -1] == 0.0  # choosing the change in force again is keeping it
    assert level[1, 0, PLANNED + 2] == pytest.approx(HABIT["switch"])  # the only other level
    assert heading[1, 1, KEEP] == pytest.approx(1 - HABIT["try"])  # the next aircraft has nothing in force


def test_held_choices_fly_as_planned_when_the_ai_keeps_everything():
    from types import SimpleNamespace

    from approach.ai.vector import VectorEnv, as_planned, holds

    counts = []
    for hold in (False, True):
        env = VectorEnv(seed=1, cached_winds_only=True, hold=hold)
        env.reset(seed=3)
        for _ in range(60):
            _, _, done, _, _ = env.step(as_planned())
            if done:
                break
        counts.append(dict(env.counts))
    assert counts[0] == counts[1]
    assert counts[1]["instructions"] == 0
    assert holds(SimpleNamespace(action_space=VectorEnv(seed=1, cached_winds_only=True, hold=True).action_space))
    assert not holds(SimpleNamespace(action_space=VectorEnv(seed=1, cached_winds_only=True).action_space))
