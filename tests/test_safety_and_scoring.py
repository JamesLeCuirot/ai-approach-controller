import importlib.util
from types import SimpleNamespace

import pytest

from approach.airports.sectorfile import DEFAULT_SECTOR_DIR
from approach.instructions import Instruction

needs_sector_file = pytest.mark.skipif(
    not (DEFAULT_SECTOR_DIR / "Navaids").is_dir(), reason="run scripts/fetch_sector_file.sh first"
)
needs_bluesky = pytest.mark.skipif(importlib.util.find_spec("bluesky") is None, reason="needs bluesky-simulator")


@pytest.fixture(scope="module")
def airport():
    from approach.airports import egkk

    return egkk.load()


def view(cs, x, y, alt, hdg=90.0, gs=250.0, along=20.0, loc=False, wind=(0.0, 0.0)):
    return SimpleNamespace(cs=cs, x=x, y=y, alt_ft=alt, hdg=hdg, gs_kt=gs, tas_kt=gs, target_hdg=hdg, target_alt=alt,
                           loc=loc, gs=False, along=along, wind_e_kt=wind[0], wind_n_kt=wind[1])


@needs_sector_file
def test_safety_rules(airport):
    from approach.controllers.safety import SafetyCheck

    safety = SafetyCheck(airport, "26L")
    me = view("A", 0, -10, 6000)
    assert not safety.review(Instruction("A", "altitude", 1500), [me])[0]
    assert safety.review(Instruction("A", "altitude", 2500), [me])[0]  # inside the minimum-altitude area
    far = view("A", 20, -15, 6000, hdg=0.0)  # outside it, near TIMBA
    ok, reason = safety.review(Instruction("A", "altitude", 2500), [far])
    assert not ok and "3,000 ft" in reason
    assert not safety.review(Instruction("A", "speed", 270), [me])[0]
    assert not safety.review(Instruction("A", "speed", 150), [me])[0]
    assert safety.review(Instruction("A", "speed", 180), [me])[0]
    assert safety.review(Instruction("A", "altitude", 4000), [me])[0]


@needs_sector_file
def test_safety_blocks_a_descent_onto_traffic_below(airport):
    from approach.controllers.safety import SafetyCheck

    safety = SafetyCheck(airport, "26L")
    upper, lower = view("UP", 0, -10, 9000), view("LO", 0.5, -10, 8000)
    ok, reason = safety.review(Instruction("UP", "altitude", 8000), [upper, lower])
    assert not ok and "LO" in reason
    # The same descent with the other aircraft 10 NM away is fine.
    far = view("LO", 10, -10, 8000)
    assert safety.review(Instruction("UP", "altitude", 8000), [upper, far])[0]
    assert safety.blocks == 1


@needs_sector_file
def test_scoring_spacing_and_losses(airport):
    from approach.scoring import score
    from approach.session import SessionLog

    rwy = "26L"
    x4, y4 = airport.centreline_point(rwy, 4.0)
    x0, y0 = airport.centreline_point(rwy, 0.0)
    log = SessionLog(meta={}, map={})
    log.add_event(0, "takeover", "A", actype="B77W")
    log.add_event(0, "takeover", "B", actype="A320")
    log.frames = [
        {"t": 0, "ac": [{"cs": "A", "x": 0, "y": 0, "alt": 5000}, {"cs": "B", "x": 1, "y": 0, "alt": 5500}]},
        {"t": 100, "ac": [{"cs": "A", "x": x0, "y": y0, "alt": 200}, {"cs": "B", "x": x4, "y": y4, "alt": 1500}]},
    ]
    log.add_event(100, "landed", "A", actype="B77W")
    log.add_event(190, "landed", "B", actype="A320")
    s = score(log, airport, rwy)
    assert s["landed"] == 2
    assert s["separation_losses"] == 1  # 1 NM and 500 ft apart in the first frame
    assert s["min_spacing_nm"] == 4.0
    assert s["wake_violations"] == 0  # a medium needs 4 NM behind a heavy


@needs_sector_file
@needs_bluesky
def test_safety_demo_blocks_the_unsafe_instructions():
    from approach.run import run_scenario
    from approach.scenarios import SCENARIOS

    log = run_scenario(SCENARIOS["safety_demo"])
    reasons = [e["reason"] for e in log.events if e["kind"] == "blocked"]
    assert len(reasons) == 5
    assert sum("within" in r for r in reasons) == 2
    assert any("minimum altitude" in r for r in reasons) and any("250 kt" in r for r in reasons)
    assert any("RMA" in r for r in reasons)
    assert log.scores["separation_losses"] == 0


@needs_sector_file
def test_safety_keeps_vectoring_inside_the_rma(airport):
    from approach.controllers.safety import SafetyCheck

    safety = SafetyCheck(airport, "26L")
    rma = airport.rma("26L")
    north_edge = view("A", 0, 4, 5000, hdg=270.0, gs=220.0, along=0.0)  # inside, close to the northern edge
    assert rma.contains(4 * 0, 4)
    ok, reason = safety.review(Instruction("A", "heading", 0.0), [north_edge])
    assert not ok and "RMA" in reason
    assert safety.review(Instruction("A", "heading", 180.0), [north_edge])[0]
    outside = view("A", 25, 18, 9000, hdg=45.0, gs=250.0, along=30.0)  # north-east of Gatwick, outside
    assert not rma.contains(25, 18)
    assert not safety.review(Instruction("A", "heading", 45.0), [outside])[0]  # away from the RMA
    assert safety.review(Instruction("A", "heading", 200.0), [outside])[0]  # towards it


@needs_sector_file
def test_scoring_counts_leaving_the_rma(airport):
    from approach.scoring import score
    from approach.session import SessionLog

    def at(t, x, y):
        return {"t": t, "ac": [{"cs": "A", "type": "A320", "wake": "M", "x": x, "y": y, "alt": 5000, "phase": "vectoring"}]}

    inside_then_out = SessionLog(meta={}, map={}, frames=[at(0, 10, -10), at(60, 10, 9), at(120, 10, 10)])
    s = score(inside_then_out, airport, "26L")
    assert s["left_rma"] == 1 and s["minutes_outside_rma"] == 1.0  # from the frame at 60 s to the last one
    never_in = SessionLog(meta={}, map={}, frames=[at(0, 10, 12), at(60, 10, 13)])
    assert score(never_in, airport, "26L")["left_rma"] == 0
