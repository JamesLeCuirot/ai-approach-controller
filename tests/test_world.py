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


def test_scripted_ils_approach_lands(airport):
    from approach.run import run_scenario
    from approach.scenarios import SCENARIOS

    log = run_scenario(SCENARIOS["ils_capture"])
    kinds = [e["kind"] for e in log.events]
    assert "landed" in kinds and "go_around" not in kinds
    texts = " ".join(e["text"] for e in log.events)
    assert "localiser captured" in texts and "glide path captured" in texts
    assert log.scores["landed"] == 1 and log.scores["stable_at_1000ft_pct"] == 100


def test_instructions_given_together_are_flown_in_order(airport):
    """A pilot never carries out the second of two headings before the first, whatever the delays."""
    from approach.sim.world import World

    for seed in range(20):
        world = World(airport, "26L", seed=seed)
        spawn_at_willo(world, airport)
        world.instruct(Instruction("TEST1", "heading", 200.0))
        world.instruct(Instruction("TEST1", "heading", 330.0))
        world.step(8)
        assert abs(world.views()[0].target_hdg - 330.0) < 0.01
        assert float(world.bs.traf.ap.trk[0]) % 360 == pytest.approx(330.0, abs=0.5), seed


def test_hold_at_willo(airport):
    """Told to hold, the pilot goes to the fix and flies the published left-hand racetrack there."""
    import math

    from approach.sim.world import World

    world = World(airport, "26L")
    fx, fy = airport.xy(airport.holds["WILLO"].position)
    world.spawn("H1", "A320", 51.1, -0.5, 150.0, 8000, 220)
    world.instruct(Instruction("H1", "hold", "WILLO"))
    turned, last, furthest = 0.0, None, 0.0
    for _ in range(900):
        world.step(1)
        v = world.views()[0]
        if world.pilots["H1"].hold_state not in ("", "to_fix"):
            furthest = max(furthest, math.hypot(v.x - fx, v.y - fy))
            if last is not None:
                turned += (v.hdg - last + 540) % 360 - 180
            last = v.hdg
    assert turned < -600  # left turns: more than one and a half laps
    assert furthest < 10.0
    assert v.phase == "holding"


def test_hold_survives_wind_after_the_outbound_turn(airport):
    """A wind that starts after the turn outbound makes the autopilot crab to keep its track, so the heading stays
    off the outbound one; the pilot still finishes the turn, flies the timed leg and comes back to the fix. (It used
    to wait for the heading and fly on outbound for good.)"""
    import math

    from approach.data.gfs import GRID_DEG, WindProfile
    from approach.sim.world import World

    world = World(airport, "26L")
    hold = airport.holds["TIMBA"]
    fx, fy = airport.xy(hold.position)
    inbound = airport.magnetic_to_true(hold.inbound_course_mag)
    outbound = (inbound + 180.0) % 360.0
    world.spawn("H1", "A320", hold.position.lat - 0.15, hold.position.lon + 0.2, inbound, 10000, 220)
    world.instruct(Instruction("H1", "hold", "TIMBA"))
    for _ in range(900):  # to the fix and round to the outbound heading, in calm air
        world.step(1)
        if world.pilots["H1"].hold_state == "turn_out" and world._last_hdg.get("H1") == pytest.approx(outbound):
            break
    assert world.pilots["H1"].hold_state == "turn_out"
    world.step(3)  # BlueSky turns the heading command into a track with the wind when it takes it: calm here
    lats = [hold.position.lat - 2 + GRID_DEG * k for k in range(17)]
    lons = [hold.position.lon - 2 + GRID_DEG * k for k in range(17)]
    grid = [[0.0] * len(lons) for _ in lats]
    world.set_wind(WindProfile(valid="", source="test", lats=lats, lons=lons, heights_ft=[30.0, 20000.0],
                               u_kt=[grid, grid], v_kt=[[[-50.0] * len(lons) for _ in lats]] * 2))  # 50 kt from the north
    furthest, back = 0.0, False
    for _ in range(900):
        world.step(1)
        v = world.views()[0]
        d = math.hypot(v.x - fx, v.y - fy)
        furthest = max(furthest, d)
        back = back or (furthest > 3.0 and d < 1.0)
    assert abs((v.hdg - v.trk + 540) % 360 - 180) > 5  # crabbing in the wind
    assert back
    assert furthest < 12.0


def test_spawning_a_callsign_already_in_the_air_is_refused(airport):
    from approach.sim.world import World

    world = World(airport, "26L")
    spawn_at_willo(world, airport, cs="DUP1")
    with pytest.raises(ValueError):
        spawn_at_willo(world, airport, cs="DUP1")


def test_wind_above_the_weather_models_top_level(airport):
    """Above the weather model's top level the wind stays as it is there (BlueSky alone has none up there)."""
    from approach.data.gfs import GRID_DEG, WindProfile
    from approach.sim.world import World

    lats = [50.0 + GRID_DEG * k for k in range(9)]
    lons = [-1.0 + GRID_DEG * k for k in range(9)]

    def grid(value):
        return [[value] * len(lons) for _ in lats]

    profile = WindProfile(valid="", source="test", lats=lats, lons=lons, heights_ft=[200.0, 6000.0, 13400.0],
                          u_kt=[grid(10.0), grid(30.0), grid(50.0)], v_kt=[grid(0.0)] * 3)
    world = World(airport, "26L", wind=profile)
    world.spawn("LOW", "A320", 51.0, 0.0, 90.0, 6000, 220)
    world.spawn("HIGH", "A320", 51.0, 0.0, 90.0, 15000, 250)
    world.step(1)
    winds = {v.cs: v.wind_e_kt for v in world.views()}
    assert winds["LOW"] == pytest.approx(30.0, abs=1.0)
    assert winds["HIGH"] == pytest.approx(50.0, abs=1.0)


def test_expedite_descent_is_faster_until_the_cleared_level(airport):
    from approach.sim.world import World, expedite_profile, descent_profile

    assert all(e >= d for e, d in zip(expedite_profile("EGKK", 0.3), descent_profile("EGKK", 0.3)))
    lost = {}
    for expedite in (False, True):
        world = World(airport, "26L", seed=3, pilots="measured")
        hold = airport.holds["WILLO"]
        world.spawn("TEST1", "A320", hold.position.lat, hold.position.lon,
                    airport.magnetic_to_true(hold.inbound_course_mag), 14000, 250)
        world.instruct(Instruction("TEST1", "altitude", 7000))
        if expedite:
            world.instruct(Instruction("TEST1", "expedite"))
            assert world.views()[0].expedite  # controllers plan with it at once
        world.step(90)
        lost[expedite] = 14000 - world.views()[0].alt_ft
        world.step(600)
        v = world.views()[0]
        assert v.alt_ft == pytest.approx(7000, abs=100) and not v.expedite  # over once it reaches its level
    assert lost[True] > lost[False] * 1.15
    assert Instruction("TEST1", "expedite").phrase() == "TEST1, expedite descent"


def test_instruction_wording_is_from_where_the_aircraft_is_now(airport):
    """Descending from 7,500 ft to 3,000 ft and stopped at 4,000 ft part way down, an aircraft is told to descend,
    not climb; and the turn is the way it will turn from the heading it is on."""
    from types import SimpleNamespace

    from approach.scenarios import Scenario
    from approach.session import SessionLog
    from approach.sim.live import LiveSession

    session = LiveSession(airport, Scenario("t", "t"), SimpleNamespace(name="test"), SessionLog({}, {}),
                          use_safety=False)
    ll = airport.grid.to_latlon(20.0, -6.0)
    session.world.spawn("W1", "A320", ll.lat, ll.lon, 270.0, 7500, 220)
    session.world.instruct(Instruction("W1", "altitude", 3000))
    session.world.instruct(Instruction("W1", "heading", 180.0))
    for _ in range(30):
        session.world.step(1)
    session.views = session.world.views()
    v = session.views[0]
    assert v.alt_ft > 4500 and v.target_alt == 3000 and 180 < v.hdg < 240
    session.instruct(Instruction("W1", "altitude", 4000))
    assert session.log.events[-1]["text"].endswith("descend altitude 4,000 ft")
    session.instruct(Instruction("W1", "heading", 250.0))  # still turning left towards 180: right from here
    assert "turn right heading" in session.log.events[-1]["text"]


def test_cancelled_approach_clearance_is_not_flown(airport):
    """Cleared for the ILS and then told the clearance is cancelled, the pilot flies its heading through the localiser
    instead of capturing it; with the clearance kept, the same pilot captures (as scenario ils_capture)."""
    from approach.sim.world import World

    def fly(cancel: bool):
        world = World(airport, "26L", seed=5)
        spawn_at_willo(world, airport, cs="EZY12AB")
        script = {5: [("heading", 75.0), ("altitude", 4000)], 300: [("altitude", 3000), ("speed", 180)],
                  420: [("heading", 315.0)], 520: [("heading", 287.0), ("cleared_ils", "26L")]}
        if cancel:
            script[530] = [("cancel_approach", "26L")]
        captured, sides = False, set()
        for t in range(800):
            for kind, value in script.get(t, []):
                world.instruct(Instruction("EZY12AB", kind, value))
            world.step(1)
            v = world.views()[0]
            captured |= bool(v.loc)
            if t > 520:
                sides.add(v.lateral > 0)
        return captured, sides, world.views()[0]

    captured, _, v = fly(cancel=False)
    assert captured and v.cleared
    captured, sides, v = fly(cancel=True)
    assert not captured and v.cleared is None and sides == {True, False}  # it flew on through the localiser
    assert Instruction("EZY12AB", "cancel_approach", "26L").phrase() == "EZY12AB, cancel approach clearance"


def test_aircraft_through_the_localiser_turned_back_from_the_north_captures_it(airport):
    """Cleared at too steep an angle to capture, the pilot flies through the localiser; turned back onto it from the
    other side at 30 degrees, still cleared, it captures it and the glide path and lands."""
    from approach.sim.world import World

    world = World(airport, "26L", seed=5)
    spawn_at_willo(world, airport, cs="EZY12AB")
    crs = airport.runways["26L"].course_true
    script = {5: [("heading", 75.0), ("altitude", 4000)], 300: [("altitude", 3000), ("speed", 180)],
              420: [("heading", (crs + 90) % 360), ("cleared_ils", "26L")]}  # across it at 90 degrees
    through_t, captured_t = None, None
    for t in range(1500):
        for kind, value in script.get(t, []):
            world.instruct(Instruction("EZY12AB", kind, value))
        world.step(1)
        views = world.views()
        if not views:
            break  # landed
        v = views[0]
        if through_t is None and not v.loc and v.lateral > 0.3:  # through, on the north side
            through_t = t
            world.instruct(Instruction("EZY12AB", "heading", (crs - 30) % 360))
        if v.loc and captured_t is None:
            captured_t, captured_nm = t, v.along
    assert through_t is not None and captured_t > through_t and captured_nm > 10.0
    assert not views  # landed


def test_a_drifting_pilot_keeps_the_spread_and_forgets_its_place_as_real_crews_do():
    """Drifting pilots (approach.sim.world): each starts exactly where it was drawn, the spread of places stays the
    measured one, and a place fades as the real ones do (a correlation of normal scores of about 0.35 a minute on,
    down to the lasting part, 0.07, after ten)."""
    import math
    import random
    from statistics import NormalDist

    import numpy as np

    from approach.sim.world import DRIFT_LASTING, drift_start, drift_step

    nd, rng = NormalDist(), random.Random(5)
    for u in (0.03, 0.5, 0.91):
        lasting, z = drift_start(u, rng)
        assert nd.cdf(math.sqrt(DRIFT_LASTING) * lasting + math.sqrt(1 - DRIFT_LASTING) * z) == pytest.approx(u, abs=1e-9)
    n = 4000
    us0 = [rng.random() for _ in range(n)]
    state = [drift_start(u, rng) for u in us0]
    at = {}
    for t in range(5, 605, 5):
        new = []
        for lasting, z in state:
            z, u = drift_step(lasting, z, 5.0, rng)
            new.append((lasting, z, u))
        state = [(a, b) for a, b, _ in new]
        if t in (60, 600):
            at[t] = np.array([u for _, _, u in new])
    z0 = np.array([nd.inv_cdf(u) for u in us0])
    for t, want in ((60, DRIFT_LASTING + (1 - DRIFT_LASTING) * math.exp(-60 / 50)), (600, DRIFT_LASTING)):
        u = at[t]
        assert abs(np.mean(u < 0.2) - 0.2) < 0.03 and abs(np.mean(u) - 0.5) < 0.02  # the spread is kept
        zt = np.array([nd.inv_cdf(min(max(x, 1e-9), 1 - 1e-9)) for x in u])
        assert np.corrcoef(z0, zt)[0, 1] == pytest.approx(want, abs=0.05)


def test_drifting_pilots_start_as_measured_ones_and_then_wander(airport):
    """Same seed: a drifting pilot is drawn exactly as a measured one (the other draws are left as they were), and
    its descent rate then wanders, where a measured pilot's place stays put."""
    from approach.sim.world import World

    pilots = {}
    for model in ("measured", "drifting"):
        world = World(airport, "26L", seed=11, pilots=model)
        spawn_at_willo(world, airport)
        p = world.pilots["TEST1"]
        start = (p.descent_u, p.bank_deg, p.decel_kt_s, p.final_slowing_kt_s)
        world.instruct(Instruction("TEST1", "altitude", 3000))
        places = set()
        for _ in range(24):
            world.step(10)
            places.add(round(p.descent_u, 3))
        pilots[model] = (start, places)
    assert pilots["measured"][0] == pilots["drifting"][0]
    assert pilots["measured"][1] == {round(pilots["measured"][0][0], 3)}
    assert len(pilots["drifting"][1]) >= 5 and all(0.0 < u < 1.0 for u in pilots["drifting"][1])
