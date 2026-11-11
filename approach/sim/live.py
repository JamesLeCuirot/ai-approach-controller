"""A session that can be advanced a second at a time.

run_session (approach.sim.session_runner) runs one straight through; the training world
(approach.ai.env) advances it ten seconds at a time and adds the AI's own instructions in
between. Both go through this class, so training and the benchmark run the same code.
"""

from __future__ import annotations

import math

from approach import scoring
from approach.airports.model import Airport
from approach.controllers.safety import SafetyCheck
from approach.instructions import Instruction
from approach.scenarios import Scenario
from approach.session import SessionLog
from approach.sim.world import World

DECISION_INTERVAL_S = 5  # how often the controller is asked
FRAME_INTERVAL_S = 4  # how often a radar frame is logged


def frame(t: float, views) -> dict:
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


class LiveSession:
    def __init__(self, airport: Airport, scenario: Scenario, controller, log: SessionLog, seed: int = 1,
                 wind=None, use_safety: bool = True, frames: bool = True, pilots: str = "bluesky",
                 lookahead: str = "old"):
        self.airport = airport
        self.scenario = scenario
        self.controller = controller
        self.log = log
        self.world = World(airport, scenario.runway, seed=seed, wind=wind, pilots=pilots)
        self.safety = SafetyCheck(airport, scenario.runway, lookahead) if use_safety else None
        self.pending = sorted(scenario.arrivals, key=lambda a: a.release_s)
        self.released = 0
        self.t = 0
        self.frames = frames
        self.views = []
        log.add_event(0, "note", None, f"{controller.name} controller, runway {scenario.runway}")
        if wind is not None:
            surface = log.meta.get("weather", {}).get("levels", [{}])[0]
            log.add_event(0, "note", None, f"Real winds from {wind.source}, valid {wind.valid}"
                          + (f": surface {surface['dir']:03d}/{surface['kt']} kt" if surface else ""))

    @property
    def done(self) -> bool:
        if self.t > self.scenario.duration_s:
            return True
        return (self.released == len(self.pending) and not self.world.pilots
                and self.t > (self.pending[-1].release_s if self.pending else 0) + 10)

    def _release(self) -> None:
        airport, world, log, t = self.airport, self.world, self.log, self.t
        while self.released < len(self.pending) and self.pending[self.released].release_s <= t:
            a = self.pending[self.released]
            level = f"FL{a.level_ft // 100}" if a.level_ft > airport.transition_altitude_ft else f"{a.level_ft:,} ft"
            if a.lat is not None:
                hdg, cas = _real_entry(a, world.wind)
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
            self.released += 1

    def instruct(self, instr: Instruction, notify_controller: bool = True) -> tuple[bool, str]:
        """Check an instruction with the safety check and pass it to the pilot; log it either way."""
        by_cs = {v.cs: v for v in self.views}
        v = by_cs.get(instr.callsign)
        if v is None:
            return False, "no such aircraft"
        phrase = instr.phrase(v.target_hdg, v.target_alt, self.airport.magnetic_variation_east,
                              self.airport.transition_altitude_ft)
        ok, reason = self.safety.review(instr, self.views) if self.safety else (True, "")
        if ok:
            self.world.instruct(instr)
            self.log.add_event(self.t, "instruction", instr.callsign, phrase, instr=instr.kind, value=instr.value)
            self.views = self.world.views()
        else:
            self.log.add_event(self.t, "blocked", instr.callsign, f"{phrase} (blocked: {reason})", instr=instr.kind,
                               reason=reason)
            if notify_controller:
                self.controller.on_event({"kind": "blocked", "instruction": instr, "reason": reason, "t": self.t})
        return ok, reason

    def tick(self) -> list[dict]:
        """One second: release arrivals, ask the controller (every 5 s), log a frame (every 4 s), move the world."""
        self._release()
        self.views = self.world.views()
        if self.t % DECISION_INTERVAL_S == 0:
            for instr in self.controller.decide(self.t, self.views):
                self.instruct(instr)
        if self.frames and self.t % FRAME_INTERVAL_S == 0:
            self.log.frames.append(frame(self.t, self.views))
        events = []
        for e in self.world.step(1.0):
            self.log.add_event(self.world.t, e.pop("kind"), e.pop("cs", None), e.pop("text", ""), **e)
            self.controller.on_event(self.log.events[-1])
            events.append(self.log.events[-1])
        self.t += 1
        return events

    def finish(self) -> SessionLog:
        self.log.frames.append(frame(self.t, self.world.views()))
        self.log.meta["duration"] = self.t
        self.log.scores = scoring.score(self.log, self.airport, self.scenario.runway)
        self.log.meta["scores_labels"] = scoring.LABELS
        return self.log
