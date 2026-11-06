import math

import pytest

from approach.airports.sectorfile import DEFAULT_SECTOR_DIR
from approach.geo import LocalGrid, LatLon, parse_sector_coord

needs_sector_file = pytest.mark.skipif(
    not (DEFAULT_SECTOR_DIR / "Navaids").is_dir(), reason="run scripts/fetch_sector_file.sh first"
)


def test_parse_sector_coord():
    assert parse_sector_coord("N051.08.45.120") == pytest.approx(51 + 8 / 60 + 45.12 / 3600)
    assert parse_sector_coord("W000.12.24.520") == pytest.approx(-(12 / 60 + 24.52 / 3600))


def test_local_grid_round_trip():
    grid = LocalGrid(LatLon(51.15, -0.19))
    x, y = grid.to_xy(51.0, 0.2)
    back = grid.to_latlon(x, y)
    assert back.lat == pytest.approx(51.0) and back.lon == pytest.approx(0.2)


@needs_sector_file
def test_gatwick_matches_the_spec():
    from approach.airports import egkk

    apt = egkk.load()
    rwy = apt.runways["26L"]
    # AIP: 26L threshold 51 09 02.42N 000 10 19.00W, true bearing 257.65
    assert rwy.threshold.lat == pytest.approx(51 + 9 / 60 + 2.42 / 3600, abs=1e-6)
    assert rwy.threshold.lon == pytest.approx(-(10 / 60 + 19.0 / 3600), abs=1e-6)
    assert rwy.course_true == pytest.approx(257.65, abs=0.1)
    # 3 degree glide path: about 318 ft per NM above the threshold datum
    assert rwy.glide_path_altitude_ft(10) - rwy.glide_path_altitude_ft(9) == pytest.approx(318.4, abs=1)
    # WILLO 50 59 05.88N 000 11 30.30W, TIMBA 50 56 43.99N 000 15 42.25E
    assert apt.fixes["WILLO"].lat == pytest.approx(50 + 59 / 60 + 5.88 / 3600, abs=1e-6)
    assert apt.fixes["TIMBA"].lon == pytest.approx(15 / 60 + 42.25 / 3600, abs=1e-6)
    assert apt.holds["WILLO"].turn == "left" and apt.holds["TIMBA"].turn == "right"
    assert "Gatwick RMA Westerlies" in apt.airspace and "EGKK Gatwick SMAA" in apt.airspace


@needs_sector_file
def test_runway_frame():
    from approach.airports import egkk

    apt = egkk.load()
    # A point 10 NM out on the 26L extended centreline, 2 NM to the left (south)
    x, y = apt.centreline_point("26L", 10.0, -2.0)
    along, lateral = apt.runway_frame("26L", x, y)
    assert along == pytest.approx(10.0, abs=1e-6) and lateral == pytest.approx(-2.0, abs=1e-6)
    # WILLO is south of the final approach and slightly west of the threshold
    along, lateral = apt.runway_frame("26L", *apt.xy(apt.fixes["WILLO"]))
    assert lateral < -9 and -4 < along < 0
    assert math.isclose(apt.magnetic_to_true(284), 285.33, abs_tol=0.01)
