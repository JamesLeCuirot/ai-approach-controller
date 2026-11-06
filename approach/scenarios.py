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
    # Real traffic: released where it really was at handover instead of at the hold fix.
    lat: float | None = None
    lon: float | None = None
    track: float | None = None  # true track over the ground
    gs_kt: float | None = None  # ground speed


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
    weather: str | None = None  # UTC hour of real weather to fly in, e.g. "2024-12-07T09" (GFS winds)


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


def _busy(count: int, every_s: float) -> list[Arrival]:
    """Arrivals alternating WILLO (FL80) and TIMBA (FL90) at a steady rate."""
    types = ["A320", "B738", "A20N", "B77W", "A321", "B38M", "E190", "A319"]
    return [
        Arrival(f"TST{i:02d}", types[i % len(types)], "WILLO" if i % 2 == 0 else "TIMBA", release_s=i * every_s,
                level_ft=8000 if i % 2 == 0 else 9000)
        for i in range(count)
    ]


register(
    Scenario(
        name="busy_30_per_hour",
        title="Stress test: 8 arrivals, one every 2 minutes",
        arrivals=_busy(8, 120),
        duration_s=3600,
        notes="About Gatwick's busiest arrival rate. The stage 0 rules controller copes: all land, no losses.",
    )
)

register(
    Scenario(
        name="busy_40_per_hour",
        title="Stress test: 12 arrivals, one every 90 seconds",
        arrivals=_busy(12, 90),
        duration_s=3600,
        notes="Faster than the stage 0 rules controller can handle without holds: it runs out of altitudes and spacing.",
    )
)

# Real winds from NOAA's GFS weather model (approach.data.gfs). Any scenario can also be flown in the
# winds of any hour since 2021 with: python -m approach.run <scenario> --weather 2024-12-07T09
STORM_DARRAGH = "2024-12-07T09"  # Gatwick reported 260/30 gusting 41 kt; 64 kt at 3,000 ft, almost straight down 26L
STORM_ISHA = "2024-01-21T21"  # Gatwick reported 210/25 gusting 38 kt; 70 kt at 3,000 ft from 219, 40 degrees across 26L

register(
    Scenario(
        name="two_arrivals_darragh",
        title="Rules controller in Storm Darragh: two arrivals, 64 kt of wind at 3,000 ft almost straight down the runway",
        arrivals=[Arrival("EZY12AB", "A320", "WILLO"), Arrival("BAW2GE", "B77W", "TIMBA", level_ft=9000)],
        duration_s=2400,
        weather=STORM_DARRAGH,
        notes="The two_arrivals traffic in the real winds of 7 December 2024, 09Z.",
    )
)

register(
    Scenario(
        name="two_arrivals_isha",
        title="Rules controller in Storm Isha: two arrivals, 70 kt of wind at 3,000 ft, 40 degrees across the runway",
        arrivals=[Arrival("EZY12AB", "A320", "WILLO"), Arrival("BAW2GE", "B77W", "TIMBA", level_ft=9000)],
        duration_s=2400,
        weather=STORM_ISHA,
        notes="The two_arrivals traffic in the real winds of 21 January 2024, 21Z.",
    )
)

register(
    Scenario(
        name="three_arrivals_isha",
        title="Rules controller in Storm Isha: three arrivals, a heavy leading two mediums",
        arrivals=[
            Arrival("BAW2GE", "B77W", "TIMBA", release_s=0, level_ft=9000),
            Arrival("EZY12AB", "A320", "WILLO", release_s=30),
            Arrival("TOM4KP", "B38M", "TIMBA", release_s=90),
        ],
        duration_s=2700,
        weather=STORM_ISHA,
        notes="The three_arrivals traffic in the real winds of 21 January 2024, 21Z.",
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
            (30, Instruction("EZY12AB", "heading", 150.0)),  # turn away, into the RMA: allowed
            (40, Instruction("EZY12AB", "altitude", 1500)),  # below the 2,000 ft minimum: blocked
            (50, Instruction("BAW2GE", "speed", 280)),  # above 250 kt: blocked
            (60, Instruction("BAW2GE", "heading", 60.0)),  # turn the other way: allowed
            (90, Instruction("EZY12AB", "heading", 240.0)),  # out of the RMA's south-west edge: blocked
            (150, Instruction("BAW2GE", "altitude", 7000)),  # now well apart: the same descent is allowed
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
