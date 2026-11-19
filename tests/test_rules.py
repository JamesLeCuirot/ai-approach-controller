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


def test_stack_check_counts_the_whole_stack():
    """With stack_check="stack", a level in a stack is free only when every aircraft below in that stack has left
    it and none in the stack is cleared to it, however far apart they are in the hold; with "near", only aircraft
    within VACATE_NM count."""
    from types import SimpleNamespace

    from approach.airports import egkk
    from approach.controllers.rules import VACATE_NM, Plan, RulesController

    ap = egkk.load()
    fx, fy = ap.xy(ap.holds["WILLO"].position)

    def plan(cs, stage="inbound"):
        p = Plan(cs, "A320", 0.0, 1.0, stage=stage)
        p.hold_fix, p.holding = "WILLO", True
        return p

    def aircraft(cs, dx, alt, target):
        return SimpleNamespace(cs=cs, x=fx + dx, y=fy, alt_ft=alt, target_alt=target)

    for check, free in (("near", 8000.0), ("stack", 10000.0)):
        c = RulesController(ap, "26L", stack_check=check)
        me, below = plan("ME"), plan("BELOW")
        c.plans = {"ME": me, "BELOW": below}
        # The aircraft below is still at 9,000 ft, on the far side of the hold from this one.
        views = {"ME": aircraft("ME", -3.5, 10000.0, 10000.0),
                 "BELOW": aircraft("BELOW", VACATE_NM - 3.0, 9000.0, 7000.0)}
        assert c._free_level(me, views["ME"], views, 8000.0) == free
    # One cleared to a level holds it, even from above.
    c = RulesController(ap, "26L", stack_check="stack")
    me, above = plan("ME"), plan("ABOVE")
    c.plans = {"ME": me, "ABOVE": above}
    views = {"ME": aircraft("ME", 0.0, 10000.0, 10000.0), "ABOVE": aircraft("ABOVE", 8.0, 12000.0, 8000.0)}
    assert c._free_level(me, views["ME"], views, 8000.0) == 9000.0


def test_straight_in_brings_an_aircraft_on_the_final_line_straight_in():
    """With straight_in, an aircraft handed over far out near the extended centreline, heading the way of the final,
    is planned straight in (its leg along the course on its own side, inside the RMA) rather than sent to its stack;
    one far off the line, or with its slot later than speed can make, is not; and one brought straight in lands
    behind every aircraft already released."""
    import math
    from types import SimpleNamespace

    from approach.airports import egkk
    from approach.controllers.rules import RMA_MARGIN_NM, Plan, RulesController

    ap = egkk.load()
    c = RulesController(ap, "26L", fixed_paths=True, speeds="slot", wind_aware=True, stretch=True, release="eager",
                        descent="plan", height_by="time", expedite=True, straight_in=True)
    crs = ap.runways["26L"].course_true

    def aircraft(cs, along, lateral, alt, hdg=None):
        x, y = ap.centreline_point("26L", along, lateral)
        return SimpleNamespace(cs=cs, actype="A320", x=x, y=y, alt_ft=alt, vs_fpm=0.0, gs_kt=270.0, tas_kt=270.0,
                               cas_kt=230.0, hdg=crs if hdg is None else hdg, trk=crs, wind_e_kt=0.0, wind_n_kt=0.0,
                               along=along, lateral=lateral, target_alt=alt, expedite=False)

    on_line = aircraft("ON", 30.0, -2.0, 7000.0)
    p = Plan("ON", "A320", 0.0, c.south)
    sp = c._straight_plan(0.0, on_line, p, need=0.0)
    assert sp is not None and sp.straight and sp.side == -1.0
    pts = c._points(sp)
    assert all(c._inside(*pts[k], margin=RMA_MARGIN_NM.get(k, 0.0)) for k in ("B", "J"))
    # its way to the leg along the course is at most 30 degrees off the final course
    to_d = math.degrees(math.atan2(pts["D"][0] - on_line.x, pts["D"][1] - on_line.y)) % 360
    assert abs((to_d - crs + 180) % 360 - 180) <= 30.5
    assert c._straight_plan(0.0, aircraft("OFF", 30.0, -12.0, 7000.0), p, need=0.0) is None
    assert c._straight_plan(0.0, aircraft("AWAY", 30.0, -2.0, 7000.0, hdg=(crs + 180) % 360), p, need=0.0) is None
    assert c._straight_plan(0.0, on_line, p, need=3600.0) is None  # its slot is an hour away
    # In the replan, it goes behind an aircraft already on its downwind, and ahead of one on its way to a stack.
    released = Plan("DW", "A320", 0.0, c.south, stage="downwind")
    released.landing_t = released.slot_t = 300.0
    waiting = Plan("IN", "A320", 10.0, c.south)
    waiting.landing_t = waiting.slot_t = 400.0
    me = Plan("ON", "A320", 20.0, c.south)
    c.plans = {"DW": released, "IN": waiting, "ON": me}
    views = {"DW": aircraft("DW", 8.0, -6.0, 4000.0), "IN": aircraft("IN", 20.0, -25.0, 12000.0), "ON": on_line}
    c._straight_pass(0.0, views)
    assert me.stage == "downwind" and me.straight and me.key > released.key and me.slot_t > released.slot_t
    assert waiting.stage == "inbound"


def test_released_order_puts_a_released_aircraft_behind_those_already_released():
    """With released_order, an aircraft released from its stack goes behind every aircraft already released, even
    if its first estimated landing (its key) was earlier."""
    from types import SimpleNamespace

    from approach.airports import egkk
    from approach.controllers.rules import Plan, RulesController

    ap = egkk.load()
    c = RulesController(ap, "26L", fixed_paths=True, released_order=True)
    first = Plan("FIRST", "A320", 200.0, c.south, stage="base")
    late = Plan("LATE", "A320", 100.0, c.south)  # estimated sooner at handover, but still holding
    c.plans = {"FIRST": first, "LATE": late}
    views = {"FIRST": SimpleNamespace(), "LATE": SimpleNamespace()}
    c.behind_released(late, views)
    assert late.key > first.key
