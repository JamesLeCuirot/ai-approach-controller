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
    if name == "rules_vector":  # stage 2's planner flying its own proposals, as "as planned" everywhere does
        from approach.ai.vector import VectorController

        return VectorController(airport, scenario.runway, name=name)
    if name.startswith("ai_vector"):  # stage 2's AI: ai_vector (the final model) or ai_vector_<checkpoint>
        import os
        from pathlib import Path

        from approach.ai.env import load_policy
        from approach.ai.vector import VectorController, holds

        tag = name[len("ai_vector_"):] or "final"
        path = os.environ.get("APPROACH_AI_MODEL") or str(Path(os.environ["APPROACH_AI_RUN"]) / f"model_{tag}.zip")
        policy = load_policy(path)
        return VectorController(airport, scenario.runway, policy=policy, name=name, hold=holds(policy))
    if name.startswith("ai_base"):  # stage 1b's AI: ai_base (the final model) or ai_base_<checkpoint>
        import os
        from pathlib import Path

        from approach.ai.env import AISpeedController, load_policy

        tag = name[len("ai_base_"):] or "final"
        path = os.environ.get("APPROACH_AI_MODEL") or str(Path(os.environ["APPROACH_AI_RUN"]) / f"model_{tag}.zip")
        return AISpeedController(airport, scenario.runway, policy=load_policy(path), name=name, base_turns=True)
    if name.startswith("ai_speed"):  # stage 1a's AI: ai_speed (the final model) or ai_speed_<checkpoint>, e.g. ai_speed_100k
        import os
        from pathlib import Path

        from approach.ai.env import AISpeedController, load_policy

        tag = name[len("ai_speed_"):] or "final"
        path = os.environ.get("APPROACH_AI_MODEL") or str(Path(os.environ["APPROACH_AI_RUN"]) / f"model_{tag}.zip")
        return AISpeedController(airport, scenario.runway, policy=load_policy(path), name=name)
    return NoController()


def world_options(controller_name: str) -> tuple[str, str, str]:
    """A controller name can end in options after a double underscore: "__measured" for pilots who fly like real
    arrivals, "__model" for the safety check's look-ahead from the model of how aircraft respond, or both
    ("rules_stretch__measured_model"). Returns the controller's own name, the pilots and the look-ahead."""
    base, _, opts = controller_name.partition("__")
    words = opts.split("_") if opts else []
    if base == "rules_vector" or base.startswith("ai_vector"):  # stage 2's world unless told otherwise
        words = words or ["measured", "model"]
    return base, "measured" if "measured" in words else "bluesky", "model" if "model" in words else "old"


def run_session(airport: Airport, scenario: Scenario, controller_name: str, log: SessionLog, seed: int = 1,
                wind=None) -> SessionLog:
    base, pilots, lookahead = world_options(controller_name)
    controller = make_controller(base, airport, scenario, seed)
    # Scripted tests send their instructions straight to the pilots unless the scenario asks
    # for the safety check (as the safety demo does).
    use_safety = scenario.safety if scenario.safety is not None else base != "script"
    session = LiveSession(airport, scenario, controller, log, seed=seed, wind=wind, use_safety=use_safety,
                          pilots=pilots, lookahead=lookahead)
    while not session.done:
        session.tick()
    return session.finish()
