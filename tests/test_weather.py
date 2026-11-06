"""Real wind: the GFS profile, BlueSky's wind field, and pilots who correct for drift."""

import importlib.util

import pytest

from approach.airports.sectorfile import DEFAULT_SECTOR_DIR
from approach.data.gfs import WindProfile, parse_time

needs_sector_file = pytest.mark.skipif(
    not (DEFAULT_SECTOR_DIR / "Navaids").is_dir(), reason="run scripts/fetch_sector_file.sh first"
)
needs_bluesky = pytest.mark.skipif(importlib.util.find_spec("bluesky") is None, reason="needs bluesky-simulator")


def uniform_wind(from_deg: float, kt: float, lat=51.15, lon=-0.19) -> WindProfile:
    """A made-up wind, the same everywhere and at every height, on a GFS-shaped grid."""
    import math

    u = -kt * math.sin(math.radians(from_deg))
    v = -kt * math.cos(math.radians(from_deg))
    lats = [round(lat - 1.5 + 0.25 * i, 2) for i in range(13)]
    lons = [round(lon - 2.0 + 0.25 * j, 2) for j in range(17)]
    grid = lambda x: [[x] * len(lons) for _ in lats]  # noqa: E731
    heights = [230, 1000, 3000, 6000, 10000, 14000]
    return WindProfile(valid="2000-01-01T00:00Z", source="test", lats=lats, lons=lons, heights_ft=heights,
                       u_kt=[grid(u)] * len(heights), v_kt=[grid(v)] * len(heights))


def test_parse_time():
    t = parse_time("2024-12-07T09")
    assert (t.year, t.month, t.day, t.hour, t.tzinfo is not None) == (2024, 12, 7, 9, True)
    assert parse_time("2024-12-07 09:00Z") == t


def test_profile_direction_and_interpolation():
    w = uniform_wind(260, 30)
    d, s = w.wind(51.1, -0.3, 4500)
    assert abs(d - 260) < 0.01 and abs(s - 30) < 0.01
    # Interpolates between levels.
    w.u_kt[1] = [[0.0] * len(w.lons) for _ in w.lats]
    w.v_kt[1] = [[0.0] * len(w.lons) for _ in w.lats]
    w.u_kt[2] = [[20.0] * len(w.lons) for _ in w.lats]
    w.v_kt[2] = [[0.0] * len(w.lons) for _ in w.lats]
    u, v = w.uv(51.1, -0.3, 2000)
    assert abs(u - 10.0) < 1e-6 and abs(v) < 1e-6


@needs_sector_file
@needs_bluesky
def test_bluesky_wind_matches_profile_and_moves_aircraft():
    from approach.airports import egkk
    from approach.sim.world import World

    airport = egkk.load()
    world = World(airport, "26L", wind=uniform_wind(270, 40))
    world.spawn("T1", "A320", 51.0, -0.6, 0.0, 6000, 220)  # heading north in a westerly
    world.step(20)
    v = world.views()[0]
    assert abs(v.wind_e_kt - 40) < 0.5 and abs(v.wind_n_kt) < 0.5
    assert 5 < v.trk - v.hdg < 12  # drifts right of its heading


@needs_sector_file
@needs_bluesky
@pytest.mark.parametrize("side", [1, -1])
def test_pilot_holds_the_localiser_in_a_strong_crosswind(side):
    from approach.airports import egkk
    from approach.sim.world import World

    airport = egkk.load()
    rwy = airport.runways["26L"]
    world = World(airport, "26L", wind=uniform_wind(210, 35))  # about 26 kt across 26L
    x, y = airport.centreline_point("26L", 14.0, 3.0 * side)
    p = airport.grid.to_latlon(x, y)
    world.spawn("T1", "A320", p.lat, p.lon, (rwy.course_true - 40 * side) % 360, 3000, 180)
    world.pilots["T1"].cleared = "26L"
    events, worst = [], 0.0
    for _ in range(900):
        events += world.step(1)
        views = world.views()
        if not views:
            break
        if views[0].along < 8 and world.pilots["T1"].loc:
            worst = max(worst, abs(views[0].lateral))
    kinds = [e["kind"] for e in events]
    assert "landed" in kinds and "go_around" not in kinds
    assert worst < 0.1
