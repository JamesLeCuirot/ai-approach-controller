import importlib.util

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


def spawn_at_willo(world, airport, cs="TEST1", actype="A320"):
    hold = airport.holds["WILLO"]
    hdg = airport.magnetic_to_true(hold.inbound_course_mag)
    world.spawn(cs, actype, hold.position.lat, hold.position.lon, hdg, 8000, 220)
    return hdg


def test_turn_left_30_degrees(airport):
    from approach.geo import bearing_xy
    from approach.sim.world import World

    world = World(airport, "26L", seed=1)
    start_hdg = spawn_at_willo(world, airport)
    world.step(5)
    v0 = world.views()[0]
    world.instruct(Instruction("TEST1", "heading", start_hdg - 30))
    headings = []
    for _ in range(40):
        world.step(1)
        headings.append(world.views()[0].hdg)
    v1 = world.views()[0]
    assert v1.hdg == pytest.approx(start_hdg - 30, abs=0.5)
    # Turn rate while turning: airliners at 220 kt bank about 25 degrees, roughly 2 deg/s
    turning = [a - b for a, b in zip(headings, headings[1:]) if abs(a - b) > 0.5]
    rate = sum(turning) / len(turning)
    assert 1.5 <= rate <= 3.5
    # The track over the ground has changed, not just the number on the aircraft
    world.step(30)
    v2 = world.views()[0]
    assert bearing_xy(v1.x, v1.y, v2.x, v2.y) == pytest.approx(start_hdg - 30, abs=1.0)
    assert bearing_xy(v0.x, v0.y, v1.x, v1.y) != pytest.approx(start_hdg - 30, abs=3.0)


def test_pilot_delay_and_descent(airport):
    from approach.sim.world import World

    world = World(airport, "26L", seed=2)
    spawn_at_willo(world, airport)
    world.instruct(Instruction("TEST1", "altitude", 5000))
    world.step(2)
    assert world.views()[0].alt_ft == pytest.approx(8000, abs=20)  # not yet: the pilot takes 3-6 s
    world.step(60)
    v = world.views()[0]
    assert 6000 < v.alt_ft < 7500 and v.vs_fpm < -1000
