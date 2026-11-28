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


def test_stack_check_holders_keeps_arrivals_above_a_holder_kept_high():
    """With stack_check="holders", an aircraft on its way to a stack is not cleared down through the level an
    aircraft holding in that stack is cleared to, however far off it still is; with "near" it is, and a holder
    cleared below the level wanted does not stop it."""
    from types import SimpleNamespace

    from approach.airports import egkk
    from approach.controllers.rules import Plan, RulesController

    ap = egkk.load()
    fx, fy = ap.xy(ap.holds["TIMBA"].position)

    def plan(cs, holding):
        p = Plan(cs, "A320", 0.0, 1.0, stage="inbound")
        p.hold_fix, p.holding = "TIMBA", holding
        return p

    def aircraft(cs, dx, alt, target, phase):
        return SimpleNamespace(cs=cs, x=fx + dx, y=fy, alt_ft=alt, target_alt=target, phase=phase)

    for check, high_holder, free in (("near", 12000.0, 8000.0), ("holders", 12000.0, 13000.0),
                                     ("holders", 7000.0, 8000.0)):
        c = RulesController(ap, "26L", stack_check=check)
        me, holder = plan("ME", False), plan("HOLDER", True)
        c.plans = {"ME": me, "HOLDER": holder}
        # A new arrival 20 NM from the fix at FL126, cleared FL130; the holder sits on the far side of the hold.
        views = {"ME": aircraft("ME", 20.0, 12600.0, 13000.0, "vectoring"),
                 "HOLDER": aircraft("HOLDER", -3.0, high_holder, high_holder, "holding")}
        assert c._free_level(me, views["ME"], views, 8000.0) == free


def test_expedite_rate_p75_plans_an_expedited_descent_on_a_quicker_crew():
    """With expedite_rate="p75", an expedited descent not yet seen is planned on the 75th percentile of the measured
    rates (quicker than the median, so less time); once seen expediting, the crew's own rate decides either way."""
    from types import SimpleNamespace

    import pytest

    from approach.airports import egkk
    from approach.controllers.rules import EXPEDITE_SETTLE_S, Plan, RulesController

    ap = egkk.load()
    v = SimpleNamespace(cs="X", alt_ft=15000.0, vs_fpm=0.0, expedite=False)
    c50 = RulesController(ap, "26L", height_by="time", expedite=True)
    c75 = RulesController(ap, "26L", height_by="time", expedite=True, expedite_rate="p75")
    t50, t75 = c50._expedite_s(v, 3000.0), c75._expedite_s(v, 3000.0)
    assert t75 < 0.9 * t50
    seen = SimpleNamespace(cs="X", alt_ft=15000.0, vs_fpm=-1500.0, expedite=True)
    p = Plan("X", "A320", 0.0, 1.0)
    p.expedite_t = 0.0
    for c in (c50, c75):
        c.now = EXPEDITE_SETTLE_S + 1.0
    assert c75._expedite_s(seen, 3000.0, p) == pytest.approx(c50._expedite_s(seen, 3000.0, p), rel=0.05)
    with pytest.raises(ValueError):
        RulesController(ap, "26L", expedite_rate="p90")


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


def test_slow_room_slows_a_fast_downwind_in_time():
    """With slow_room, a downwind speed is no faster than one the aircraft can still slow from to 180 kt by its join
    point at a slow pilot's rate: 250 kt far out, less as the join point nears, and never below the band."""
    from types import SimpleNamespace

    from approach.airports import egkk
    from approach.controllers.rules import Plan, RulesController

    ap = egkk.load()
    c = RulesController(ap, "26L", fixed_paths=True, speeds="slot", slow_room=True)
    p = Plan("DW", "A320", 0.0, c.south, stage="downwind", d_join=11.0)
    view = lambda along, lat: SimpleNamespace(  # noqa: E731
        *(), **dict(zip(("x", "y"), ap.centreline_point("26L", along, lat))), gs_kt=270.0, cas_kt=250.0)
    limits = (180, 250)
    far, near, corner = view(40.0, c.south * 6.0), view(20.0, c.south * 6.0), view(14.6, c.south * 6.0)
    assert c._room_to_slow(far, p, 250, limits) == 250
    assert 220 < c._room_to_slow(near, p, 250, limits) < 250
    assert c._room_to_slow(corner, p, 250, limits) <= 220  # at its base turn: about 8 NM left to slow to 180
    assert c._room_to_slow(corner, p, 200, limits) == 200
    assert c._room_to_slow(near, Plan("B", "A320", 0.0, c.south, stage="base"), 180, (160, 180)) == 180
    assert RulesController(ap, "26L", fixed_paths=True, speeds="slot")._room_to_slow(near, p, 250, limits) == 250


def test_final_room_slows_the_final_in_time_for_a_slow_pilot():
    """With final_room, a speed on the intercept or final is no faster than one the slowest pilot can slow from to
    160 kt by 5 NM out: 180 kt far out, 170 nearer, 160 by about 7 NM; off, 180 to the end of the band."""
    from types import SimpleNamespace

    from approach.airports import egkk
    from approach.controllers.rules import Plan, RulesController

    ap = egkk.load()
    c = RulesController(ap, "26L", fixed_paths=True, speeds="slot", final_room=True)
    p = Plan("FI", "A320", 0.0, c.south, stage="cleared")
    view = lambda along: SimpleNamespace(along=along, gs_kt=180.0, cas_kt=180.0)  # noqa: E731
    limits = (160, 180)
    assert c._room_to_slow(view(12.0), p, 180, limits) == 180
    assert c._room_to_slow(view(8.0), p, 180, limits) == 170
    assert c._room_to_slow(view(7.1), p, 180, limits) == 160
    assert c._room_to_slow(view(8.0), p, 160, limits) == 160
    tail = SimpleNamespace(along=9.0, gs_kt=216.0, cas_kt=180.0)  # a tailwind: more miles flown while slowing
    assert c._room_to_slow(tail, p, 180, limits) == 170
    assert RulesController(ap, "26L", fixed_paths=True, speeds="slot")._room_to_slow(view(7.1), p, 180, limits) == 180


def test_release_first_outside_the_rma_only_slow_and_low_enough():
    """release_where="first": outside the RMA, released only when no faster than 250 kt, on its downwind's side of the
    final approach course and able to get down on its usual descent; otherwise it waits (planned on an expedited
    descent, one was still 200 kt and 1,500 ft high at 12 NM; one from the far side went round at 246 kt)."""
    from types import SimpleNamespace

    from approach.airports import egkk
    from approach.controllers.rules import Plan, RulesController

    ap = egkk.load()
    c = RulesController(ap, "26L", fixed_paths=True, speeds="slot", height_by="time", release_where="first")
    p = Plan("FI", "A320", 0.0, c.south, stage="downwind")
    p.d_join = 11.0

    def view(alt, cas, lateral, vs=-1500.0):
        x, y = ap.centreline_point("26L", 35.0, lateral)
        return SimpleNamespace(x=x, y=y, alt_ft=alt, cas_kt=cas, tas_kt=cas * (1 + alt / 60000.0), gs_kt=cas * 1.2,
                               vs_fpm=vs, expedite=False, wind_e_kt=0.0, wind_n_kt=0.0, lateral=lateral)

    near_side = 12.0 * c.south
    assert not c._inside(view(8000.0, 240.0, near_side).x, view(8000.0, 240.0, near_side).y)
    assert c._outside_ok(view(8000.0, 240.0, near_side), p, 11.0)
    assert not c._outside_ok(view(8000.0, 300.0, near_side), p, 11.0)  # too fast
    assert not c._outside_ok(view(8000.0, 240.0, -near_side), p, 11.0)  # on the far side of the final approach course
    assert not c._outside_ok(view(30000.0, 250.0, near_side, vs=0.0), p, 11.0)  # too high for its usual descent


def test_cancel_missed_cancels_the_clearance_of_an_aircraft_that_missed_the_localiser():
    """With cancel_missed, an aircraft cleared for the ILS that crosses to the far side of the final approach line
    without capturing is told its clearance is cancelled as it is brought round again; without, it keeps it."""
    from approach.airports import egkk
    from approach.controllers.rules import Plan, RulesController
    from approach.sim.world import AircraftView

    ap = egkk.load()
    crs = ap.runways["26L"].course_true
    for cancel in (False, True):
        c = RulesController(ap, "26L", fixed_paths=True, speeds="slot", wind_aware=True, stretch=True,
                            release="eager", descent="plan", height_by="time", cancel_missed=cancel)
        p = Plan("MISS", "A320", 0.0, c.south, stage="cleared")
        c.plans = {"MISS": p}
        lateral = -1.5 * p.side  # 1.5 NM on the far side
        x, y = ap.centreline_point("26L", 14.0, lateral)
        v = AircraftView("MISS", "A320", "M", x, y, 4000.0, 180.0, 180.0, 190.0, (crs + 60) % 360, (crs + 60) % 360,
                         0.0, 0.0, 0.0, "vectoring", "26L", 14.0, lateral, 0.0, (crs + 30) % 360, 4000.0, 180.0,
                         False, False)
        out = c.decide(100.0, [v])
        kinds = [i.kind for i in out if i.callsign == "MISS"]
        assert p.resequenced and p.stage == "downwind"
        assert ("cancel_approach" in kinds) == cancel, kinds


def test_overshoot_rejoin_turns_an_aircraft_through_the_localiser_back_onto_it_from_the_north():
    """With overshoot="rejoin", an aircraft brought onto final from the south that goes 0.3 NM through the localiser
    without catching it is turned back at 30 degrees from the north side, keeping its clearance; if it would catch it
    too close in, too high or too near the aircraft behind, or misses again, or the turn back is blocked, it is
    brought round (its clearance cancelled with cancel_missed)."""
    from approach.airports import egkk
    from approach.controllers.rules import Plan, RulesController
    from approach.instructions import Instruction
    from approach.sim.world import AircraftView

    ap = egkk.load()
    for rwy in ("26L", "08R"):
        crs = ap.runways[rwy].course_true

        def setup(along=14.0, through=0.4, alt=3000.0, behind_s=None):
            c = RulesController(ap, rwy, fixed_paths=True, speeds="slot", wind_aware=True, stretch=True,
                                release="eager", descent="plan", height_by="time", cancel_missed=True,
                                overshoot="rejoin")
            p = Plan("OVER", "A320", 0.0, c.south, stage="cleared")
            p.landing_t = 400.0
            c.plans = {"OVER": p}
            if behind_s is not None:
                q = Plan("NEXT", "A320", 10.0, c.south, stage="downwind")
                q.landing_t = 400.0 + behind_s
                c.plans["NEXT"] = q
            return c, p, view(c, along, -through * p.side, alt)

        def view(c, along, lateral, alt, hdg=None):
            x, y = ap.centreline_point(rwy, along, lateral)
            hdg = (crs - c.south * 30) % 360 if hdg is None else hdg  # its intercept heading from the south
            return AircraftView("OVER", "A320", "M", x, y, alt, 180.0, 180.0, 190.0, hdg, hdg, 0.0, 0.0, 0.0,
                                "vectoring", rwy, along, lateral, 0.0, hdg, alt, 180.0, False, False)

        def said(c, v, t=100.0):
            return {i.kind: i.value for i in c.decide(t, [v]) if i.callsign == "OVER"}

        c, p, v = setup()
        out = said(c, v)
        back = (crs + c.south * 30) % 360  # from the north side, 30 degrees the other way
        assert p.stage == "cleared" and not p.resequenced and p.rejoin_side == -c.south, rwy
        assert abs((out["heading"] - back + 180) % 360 - 180) <= 2.5 and "cancel_approach" not in out, (rwy, out)
        north = ap.centreline_point(rwy, 14.0, -c.south)[1] > ap.centreline_point(rwy, 14.0, c.south)[1]
        assert north  # the far side is the north side, on both runways
        assert said(c, view(c, 13.0, -0.8 * p.side, 3000.0, hdg=back)) == {}  # on its way back: left alone
        # it goes through again, back to the south side: brought round, its clearance cancelled
        out = said(c, view(c, 11.0, 1.2 * p.side, 3000.0, hdg=back), t=160.0)
        assert p.resequenced and p.stage == "downwind" and "cancel_approach" in out, rwy
        # brought round at once: too close in, too high
        for kw in ({"along": 7.0}, {"alt": 4500.0}):
            c, p, v = setup(**kw)
            out = said(c, v)
            assert p.resequenced and "cancel_approach" in out, (rwy, kw)
        # not turned back if it would land within the spacing minimum of the aircraft behind
        c, p, v = setup(behind_s=60.0)
        assert c._rejoin_heading(100.0, p, v) is None, rwy
        c, p, v = setup(behind_s=400.0)
        assert c._rejoin_heading(100.0, p, v) is not None, rwy
        # the turn back blocked by the safety check: brought round
        c, p, v = setup()
        said(c, v)
        c.on_event({"kind": "blocked", "cs": "OVER", "t": 105.0,
                    "instruction": Instruction("OVER", "heading", back), "reason": "test"})
        out = said(c, v, t=110.0)
        assert p.resequenced and "cancel_approach" in out, rwy


def _fd_view(ap, runway, along, lateral, hdg, alt=5000.0, cas=220.0):
    from types import SimpleNamespace

    x, y = ap.centreline_point(runway, along, lateral)
    return SimpleNamespace(cs="FD1", actype="A320", x=x, y=y, alt_ft=alt, vs_fpm=0.0, gs_kt=cas * 1.08,
                           tas_kt=cas * 1.08, cas_kt=cas, hdg=hdg, trk=hdg, wind_e_kt=0.0, wind_n_kt=0.0, along=along,
                           lateral=lateral, target_alt=alt, target_spd=cas, expedite=False, loc=False)


def test_downwind_fresh_shortens_a_lengthened_downwind_again():
    """With downwind="fresh" a downwind lengthened earlier is shortened again once nothing asks for it: to the final
    it would have if it turned base now (an aircraft flying out along the downwind), never below what the look-ahead
    asked for; "kept" (as before) never shortens it."""
    import pytest

    from approach.airports import egkk
    from approach.controllers.rules import D_JOIN_FIXED_NM, Plan, RulesController

    ap = egkk.load()
    out = (ap.runways["26L"].course_true + 180.0) % 360.0
    kw = dict(fixed_paths=True, speeds="slot", wind_aware=True, stretch=True, release="eager", descent="plan",
              height_by="time")
    kept, fresh = RulesController(ap, "26L", **kw), RulesController(ap, "26L", downwind="fresh", **kw)
    v = _fd_view(ap, "26L", 15.0, 6.0 * fresh.south, out)  # flying out along the downwind, past the 11 NM turn point
    p = Plan("FD1", "A320", 0.0, fresh.south, stage="downwind", d_join=14.5)
    now = fresh.final_if_turned_now(v, p)
    assert D_JOIN_FIXED_NM < now < 14.5
    assert kept._stretched_join(0.0, v, p, None, None) == 14.5
    assert fresh._stretched_join(0.0, v, p, None, None) == pytest.approx(now)
    p.min_join = 14.0
    assert fresh._stretched_join(0.0, v, p, None, None) == 14.0
    p.min_join = 0.0
    inbound = _fd_view(ap, "26L", 20.0, 12.0 * fresh.south, (out + 150.0) % 360.0)  # on its way to the turn point
    assert fresh._stretched_join(0.0, inbound, p, None, None) == D_JOIN_FIXED_NM
    with pytest.raises(ValueError):
        RulesController(ap, "26L", downwind="short", **kw)


def test_eta_speeds_counts_the_first_turn_and_the_room_to_slow_down():
    """With eta="speeds" an aircraft pointing away from its turn point is over a minute later than one at the same
    place pointing at it (the straight-line estimate gives both the same), and flying the slowest speed of each
    stage's band takes longer than the standard speeds: the room speed has to take a delay."""
    import pytest

    from approach.airports import egkk
    from approach.controllers.rules import Plan, RulesController, bearing_xy

    ap = egkk.load()
    kw = dict(fixed_paths=True, speeds="slot", wind_aware=True, stretch=True, release="eager", descent="plan",
              height_by="time")
    legs, spd = RulesController(ap, "08R", **kw), RulesController(ap, "08R", eta="speeds", **kw)
    p = Plan("FD1", "A320", 0.0, spd.south, stage="downwind")
    here = _fd_view(ap, "08R", 0.0, 10.0 * spd.south, 0.0)
    to_d = bearing_xy(here.x, here.y, *spd._points(p)["D"])
    at_it = _fd_view(ap, "08R", 0.0, 10.0 * spd.south, to_d)
    away = _fd_view(ap, "08R", 0.0, 10.0 * spd.south, (to_d + 180.0) % 360.0)
    assert legs._eta(away, p) == pytest.approx(legs._eta(at_it, p))
    assert spd._eta(away, p) - spd._eta(at_it, p) > 60.0
    assert 40.0 < spd._eta_speeds(at_it, p, slowest=True) - spd._eta_speeds(at_it, p) < 150.0
    assert abs(spd._eta(at_it, p) - legs._eta(at_it, p)) < 60.0
    with pytest.raises(ValueError):
        RulesController(ap, "08R", eta="fast", **kw)
    with pytest.raises(ValueError):
        RulesController(ap, "08R", release_share=0.8, **kw)


def test_fill_gives_a_waiting_aircraft_the_earliest_place_that_delays_nobody():
    """With fill=True an aircraft holding takes a place ahead of a released aircraft still far away on its downwind
    when it can land the gap ahead of it; not when there isn't room, never ahead of one on base, and never ahead of
    an aircraft holding ahead of it in its own stack."""
    from approach.airports import egkk
    from approach.controllers.rules import D_JOIN_FIXED_NM, Plan, RulesController

    ap = egkk.load()
    c = RulesController(ap, "08R", fixed_paths=True, speeds="slot", wind_aware=True, stretch=True, release="eager",
                        descent="plan", height_by="time", eta="speeds", fill=True)
    here = _fd_view(ap, "08R", 0.0, 10.0 * c.south, 270.0, alt=7000.0)
    views = {cs: here for cs in ("FD1", "BASE", "FAR", "SAME")}
    p = Plan("FD1", "A320", 0.0, c.south, stage="inbound", holding=True, hold_fix="WILLO")
    soon = c._eta(here, Plan(**{**vars(p), "stage": "downwind"}), D_JOIN_FIXED_NM)
    gap = c._gap_s("A320", p, here)
    on_base = [soon - 2 * gap, "A320", Plan("BASE", "A320", 0.0, c.south, stage="base"), True, 0.0]

    def far(when, room=0.0):
        return [when, "A320", Plan("FAR", "A320", 0.0, c.south, stage="downwind"), True, room]

    assert c._line_place([on_base, far(soon + 3 * gap)], 0.0, views, p) == 1  # into the hole ahead of the far one
    assert c._line_place([on_base, far(soon + 0.5 * gap)], 0.0, views, p) == 2  # no room: behind it
    assert c._line_place([on_base, far(soon + 0.5 * gap, room=gap)], 0.0, views, p) == 1  # it can slow down for it
    assert c._line_place([far(soon + 3 * gap), on_base], 0.0, views, p) == 2  # never ahead of one on base
    ahead = [soon + 3 * gap, "A320", Plan("SAME", "A320", 0.0, c.south, stage="inbound", holding=True,
                                           hold_fix="WILLO"), False, 0.0]
    assert c._line_place([on_base, ahead], 0.0, views, p) == 2  # its own stack keeps its order
    ahead[2].holding = False  # still on its way to the stack: passed by one already holding in it
    assert c._line_place([on_base, ahead], 0.0, views, p) == 1
