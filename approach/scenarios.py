"""Scenarios: the traffic (and any scripted instructions) for one session."""

from __future__ import annotations

from dataclasses import dataclass, field

from approach.instructions import Instruction


@dataclass
class Arrival:
    callsign: str
    actype: str
    fix: str  # hold fix where the aircraft is released to the AI (WILLO or TIMBA)
    release_s: float = 0.0
    level_ft: int = 8000  # minimum stack level + 1 (FL80) on a standard-pressure day
    speed_kt: int = 220


@dataclass
class Scenario:
    name: str
    title: str
    runway: str = "26L"
    arrivals: list[Arrival] = field(default_factory=list)
    duration_s: float = 1800.0
    controller: str = "rules"
    script: list[tuple[float, Instruction]] = field(default_factory=list)  # scripted instructions (tests, demos)
    notes: str = ""
    safety: bool | None = None  # safety check on? Default: on for every controller except scripts


SCENARIOS: dict[str, Scenario] = {}


def register(s: Scenario) -> Scenario:
    SCENARIOS[s.name] = s
    return s


register(
    Scenario(
        name="map",
        title="Gatwick airspace from the sector file",
        duration_s=20,
        controller="none",
        notes="No traffic: the runway, holds, fixes, vectoring areas and minimum-altitude areas.",
    )
)

register(
    Scenario(
        name="turn_test",
        title="Turn test: instructions change where the aircraft goes",
        duration_s=180,
        controller="script",
        arrivals=[Arrival("EZY12AB", "A320", "WILLO")],
        script=[
            (10, Instruction("EZY12AB", "heading", 255.3)),  # 30 degrees left of the WILLO inbound course (285.3 true)
            (70, Instruction("EZY12AB", "altitude", 5000)),
            (70, Instruction("EZY12AB", "speed", 200)),
            (100, Instruction("EZY12AB", "heading", 345.0)),
        ],
        notes="One aircraft at WILLO given a left turn, a descent, a speed reduction and a right turn.",
    )
)

register(
    Scenario(
        name="ils_capture",
        title="ILS capture: one arrival flown onto 26L by script",
        duration_s=1200,
        controller="script",
        arrivals=[Arrival("EZY12AB", "A320", "WILLO")],
        script=[
            (5, Instruction("EZY12AB", "heading", 75.0)),
            (5, Instruction("EZY12AB", "altitude", 4000)),
            (300, Instruction("EZY12AB", "altitude", 3000)),
            (300, Instruction("EZY12AB", "speed", 180)),
            (420, Instruction("EZY12AB", "heading", 315.0)),
            (520, Instruction("EZY12AB", "heading", 287.0)),
            (520, Instruction("EZY12AB", "cleared_ils", "26L")),
        ],
        notes="Scripted vectors east, a base turn and a 30 degree intercept; the pilot captures the localiser and glide path.",
    )
)

register(
    Scenario(
        name="one_arrival",
        title="Rules controller: one arrival from WILLO",
        arrivals=[Arrival("EZY12AB", "A320", "WILLO")],
        duration_s=1500,
    )
)

register(
    Scenario(
        name="two_arrivals",
        title="Rules controller: two arrivals released together at WILLO and TIMBA",
        arrivals=[Arrival("EZY12AB", "A320", "WILLO"), Arrival("BAW2GE", "B77W", "TIMBA", level_ft=9000)],
        duration_s=1800,
    )
)

register(
    Scenario(
        name="three_arrivals",
        title="Rules controller: three arrivals, a heavy leading two mediums",
        arrivals=[
            Arrival("BAW2GE", "B77W", "TIMBA", release_s=0, level_ft=9000),
            Arrival("EZY12AB", "A320", "WILLO", release_s=30),
            Arrival("TOM4KP", "B38M", "TIMBA", release_s=90),
        ],
        duration_s=2100,
    )
)

register(
    Scenario(
        name="safety_demo",
        title="Safety check: unsafe instructions are blocked",
        arrivals=[
            Arrival("EZY12AB", "A320", "WILLO", level_ft=8000),
            Arrival("BAW2GE", "B77W", "WILLO", level_ft=9000),  # stacked 1,000 ft above at the same fix
        ],
        duration_s=300,
        controller="script",
        safety=True,
        script=[
            (10, Instruction("BAW2GE", "altitude", 8000)),  # straight down onto EZY12AB: blocked
            (20, Instruction("EZY12AB", "altitude", 9000)),  # straight up into BAW2GE: blocked
            (30, Instruction("EZY12AB", "heading", 200.0)),  # turn away: allowed
            (40, Instruction("EZY12AB", "altitude", 1500)),  # below the 2,000 ft minimum: blocked
            (50, Instruction("BAW2GE", "speed", 280)),  # above 250 kt: blocked
            (60, Instruction("BAW2GE", "heading", 250.0)),  # turn the other way: allowed
            (150, Instruction("BAW2GE", "altitude", 7000)),  # now 6 NM apart: the same descent is allowed
        ],
        notes="Two aircraft stacked at WILLO. Scripted instructions, some unsafe on purpose, all passed through the safety check.",
    )
)

register(
    Scenario(
        name="random_controller",
        title="Random controller with the safety check on",
        arrivals=[Arrival("EZY12AB", "A320", "WILLO"), Arrival("BAW2GE", "B77W", "TIMBA", level_ft=9000)],
        duration_s=900,
        controller="random",
        notes="The random baseline: one random instruction every 15 s. The safety check blocks the unsafe ones. Nothing is meant to land.",
    )
)
