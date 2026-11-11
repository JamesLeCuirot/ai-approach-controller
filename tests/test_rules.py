import importlib.util

import pytest

from approach.airports.sectorfile import DEFAULT_SECTOR_DIR

pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("bluesky") is None or not (DEFAULT_SECTOR_DIR / "Navaids").is_dir(),
    reason="needs bluesky-simulator and the sector file",
)


def run(name):
    from approach.run import run_scenario
    from approach.scenarios import SCENARIOS

    return run_scenario(SCENARIOS[name])


def test_rules_controller_lands_one_arrival():
    s = run("one_arrival").scores
    assert s["landed"] == 1 and s["go_arounds"] == 0 and s["stable_at_1000ft_pct"] == 100


def test_rules_controller_spaces_two_arrivals():
    """Stage 0 gate: two arrivals released together land safely, at least 3 NM apart (arrivals only, no departures)."""
    s = run("two_arrivals").scores
    assert s["landed"] == 2
    assert s["separation_losses"] == 0 and s["wake_violations"] == 0
    assert s["min_spacing_nm"] >= 3.0


def test_rules_controller_three_arrivals_behind_a_heavy():
    s = run("three_arrivals").scores
    assert s["landed"] == 3
    assert s["separation_losses"] == 0 and s["wake_violations"] == 0
    assert s["min_spacing_nm"] >= 3.0
    # It keeps everyone inside the RMA and above the minimum altitudes, holding when it must.
    assert s["left_rma"] == 0 and s["below_minimum_altitude"] == 0


def test_planned_descent_lengthens_the_final_for_a_high_aircraft():
    """Stage 2: with pilots descending at real rates, a high aircraft gets a longer final, and one that can't get
    below the glide path on any path inside the RMA stays in the hold."""
    from types import SimpleNamespace

    from approach.airports import egkk
    from approach.controllers.rules import D_JOIN_MIN_NM, Plan, RulesController

    ap = egkk.load()
    c = RulesController(ap, "26L", wind_aware=True, descent="plan")
    longest = c.max_join(c.south)
    x, y = ap.centreline_point("26L", 8.0, 6.0 * c.south)  # on the downwind, before its turn point
    p = Plan("T1", "A320", 0.0, c.south, stage="downwind")
    low = SimpleNamespace(x=x, y=y, alt_ft=4000.0, vs_fpm=0.0, gs_kt=220.0)
    high = SimpleNamespace(x=x, y=y, alt_ft=7000.0, vs_fpm=0.0, gs_kt=220.0)
    assert c._join_for_height(low, p, D_JOIN_MIN_NM, longest) == D_JOIN_MIN_NM
    assert c._join_for_height(high, p, D_JOIN_MIN_NM, longest) > D_JOIN_MIN_NM
    very_high = SimpleNamespace(x=x, y=y, alt_ft=12000.0, vs_fpm=0.0, gs_kt=220.0)
    assert not c._height_ok(very_high, p, longest)
