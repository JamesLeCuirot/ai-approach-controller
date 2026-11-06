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
    """Stage 0 gate: two arrivals released together land safely, at least 6 NM apart."""
    s = run("two_arrivals").scores
    assert s["landed"] == 2
    assert s["separation_losses"] == 0 and s["wake_violations"] == 0
    assert s["min_spacing_nm"] >= 6.0


def test_rules_controller_three_arrivals_behind_a_heavy():
    s = run("three_arrivals").scores
    assert s["landed"] == 3
    assert s["separation_losses"] == 0 and s["wake_violations"] == 0
    assert s["min_spacing_nm"] >= 6.0
    # It keeps everyone inside the RMA and above the minimum altitudes, holding when it must.
    assert s["left_rma"] == 0 and s["below_minimum_altitude"] == 0
