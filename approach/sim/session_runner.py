"""Run one session: release arrivals, ask the controller, check its instructions, log everything."""

from __future__ import annotations

import math

from approach import scoring
from approach.airports.model import Airport
from approach.controllers.base import NoController, RandomController, ScriptController
from approach.controllers.safety import SafetyCheck
from approach.scenarios import Scenario
from approach.session import SessionLog
from approach.sim.world import World

DECISION_INTERVAL_S = 5
FRAME_INTERVAL_S = 4


def make_controller(name: str, airport: Airport, scenario: Scenario, seed: int):
    if name == "script":
        return ScriptController(scenario.script)
    if name == "random":
        return RandomController(seed)
    if name == "rules":
        from approach.controllers.rules import RulesController

        return RulesController(airport, scenario.runway)
    if name == "rules_speed":  # stage 1a's competitor: fixed paths, speed only
        from approach.controllers.rules import RulesController

        c = RulesController(airport, scenario.runway, fixed_paths=True, speeds="slot")
        c.name = "rules_speed"
        return c
    return NoController()


def _frame(t: float, views) -> dict:
    return {
        "t": t,
        "ac": [
            {"cs": v.cs, "type": v.actype, "wake": v.wake, "x": round(v.x, 3), "y": round(v.y, 3),
             "alt": round(v.alt_ft), "cas": round(v.cas_kt), "gs": round(v.gs_kt), "hdg": round(v.hdg, 1),
             "vs": round(v.vs_fpm), "phase": v.phase}
            for v in views
        ],
    }


def _compass(x: float, y: float) -> str:
    brg = (math.degrees(math.atan2(x, y)) + 360) % 360
    return "N NE E SE S SW W NW".split()[int(((brg + 22.5) % 360) // 45)]


def _real_entry(a, wind) -> tuple[float, float]:
    """Heading and indicated airspeed that give a real arrival's ground track and speed in the wind."""
    from bluesky.tools.aero import ft, kts, vtas2cas

    trk = math.radians(a.track or 0.0)
    ge, gn = (a.gs_kt or 250.0) * math.sin(trk), (a.gs_kt or 250.0) * math.cos(trk)
    we, wn = wind.uv(a.lat, a.lon, a.level_ft) if wind is not None else (0.0, 0.0)
    ae, an = ge - we, gn - wn
    hdg = math.degrees(math.atan2(ae, an)) % 360
    tas = math.hypot(ae, an)
    cas = float(vtas2cas(tas * kts, a.level_ft * ft)) / kts
    return hdg, max(170.0, min(320.0, cas))


def run_session(airport: Airport, scenario: Scenario, controller_name: str, log: SessionLog, seed: int = 1,
                wind=None) -> SessionLog:
    world = World(airport, scenario.runway, seed=seed, wind=wind)
    controller = make_controller(controller_name, airport, scenario, seed)
    # Scripted tests send their instructions straight to the pilots unless the scenario asks
    # for the safety check (as the safety demo does).
    use_safety = scenario.safety if scenario.safety is not None else controller_name != "script"
    safety = SafetyCheck(airport, scenario.runway) if use_safety else None
    pending = sorted(scenario.arrivals, key=lambda a: a.release_s)
    released = 0
    t = 0
    log.add_event(0, "note", None, f"{controller.name} controller, runway {scenario.runway}")
    if wind is not None:
        surface = log.meta.get("weather", {}).get("levels", [{}])[0]
        log.add_event(0, "note", None, f"Real winds from {wind.source}, valid {wind.valid}"
                      + (f": surface {surface['dir']:03d}/{surface['kt']} kt" if surface else ""))
    while t <= scenario.duration_s:
        while released < len(pending) and pending[released].release_s <= t:
            a = pending[released]
            level = f"FL{a.level_ft // 100}" if a.level_ft > airport.transition_altitude_ft else f"{a.level_ft:,} ft"
            if a.lat is not None:
                hdg, cas = _real_entry(a, wind)
                world.spawn(a.callsign, a.actype, a.lat, a.lon, hdg, a.level_ft, cas)
                x, y = airport.grid.to_xy(a.lat, a.lon)
                where = f"{(x * x + y * y) ** 0.5:.0f} NM {_compass(x, y)}"
                log.add_event(t, "takeover", a.callsign, f"{a.callsign} ({a.actype}) handed over {where}, {level}, "
                              f"{cas:.0f} kt", actype=a.actype, fix=a.fix)
            else:
                hold = airport.holds[a.fix]
                pos = hold.position
                world.spawn(a.callsign, a.actype, pos.lat, pos.lon, airport.magnetic_to_true(hold.inbound_course_mag),
                            a.level_ft, a.speed_kt)
                log.add_event(t, "takeover", a.callsign, f"{a.callsign} ({a.actype}) released at {a.fix}, {level}, "
                              f"{a.speed_kt} kt", actype=a.actype, fix=a.fix)
            released += 1
        views = world.views()
        if t % DECISION_INTERVAL_S == 0:
            by_cs = {v.cs: v for v in views}
            for instr in controller.decide(t, views):
                v = by_cs.get(instr.callsign)
                if v is None:
                    continue
                phrase = instr.phrase(v.target_hdg, v.target_alt, airport.magnetic_variation_east,
                                      airport.transition_altitude_ft)
                ok, reason = safety.review(instr, views) if safety else (True, "")
                if ok:
                    world.instruct(instr)
                    log.add_event(t, "instruction", instr.callsign, phrase, instr=instr.kind, value=instr.value)
                    views = world.views()
                else:
                    log.add_event(t, "blocked", instr.callsign, f"{phrase} (blocked: {reason})", instr=instr.kind,
                                  reason=reason)
                    controller.on_event({"kind": "blocked", "instruction": instr, "reason": reason, "t": t})
        if t % FRAME_INTERVAL_S == 0:
            log.frames.append(_frame(t, views))
        for e in world.step(1.0):
            log.add_event(world.t, e.pop("kind"), e.pop("cs", None), e.pop("text", ""), **e)
            controller.on_event(log.events[-1])
        t += 1
        if released == len(pending) and not world.pilots and t > pending[-1].release_s + 10:
            break
    log.frames.append(_frame(t, world.views()))
    log.meta["duration"] = t
    log.scores = scoring.score(log, airport, scenario.runway)
    log.meta["scores_labels"] = scoring.LABELS
    return log
