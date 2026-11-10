"""Run one session: release arrivals, ask the controller, check its instructions, log everything."""

from __future__ import annotations

from approach.airports.model import Airport
from approach.controllers.base import NoController, RandomController, ScriptController
from approach.scenarios import Scenario
from approach.session import SessionLog
from approach.sim.live import DECISION_INTERVAL_S, FRAME_INTERVAL_S, LiveSession, frame  # noqa: F401


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
    if name.startswith("rules_"):  # a named version of the rules controller (approach.controllers.versions)
        from approach.controllers.rules import RulesController
        from approach.controllers.versions import VERSIONS

        if name[len("rules_"):] in VERSIONS:
            c = RulesController(airport, scenario.runway, **VERSIONS[name[len("rules_"):]])
            c.name = name
            return c
    if name.startswith("ai_speed"):  # stage 1a's AI: ai_speed (the final model) or ai_speed_<checkpoint>, e.g. ai_speed_100k
        import os
        from pathlib import Path

        from approach.ai.env import AISpeedController, load_policy

        tag = name[len("ai_speed_"):] or "final"
        path = os.environ.get("APPROACH_AI_MODEL") or str(Path(os.environ["APPROACH_AI_RUN"]) / f"model_{tag}.zip")
        return AISpeedController(airport, scenario.runway, policy=load_policy(path), name=name)
    return NoController()


def run_session(airport: Airport, scenario: Scenario, controller_name: str, log: SessionLog, seed: int = 1,
                wind=None) -> SessionLog:
    controller = make_controller(controller_name, airport, scenario, seed)
    # Scripted tests send their instructions straight to the pilots unless the scenario asks
    # for the safety check (as the safety demo does).
    use_safety = scenario.safety if scenario.safety is not None else controller_name != "script"
    session = LiveSession(airport, scenario, controller, log, seed=seed, wind=wind, use_safety=use_safety)
    while not session.done:
        session.tick()
    return session.finish()
