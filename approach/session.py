"""Session logs: everything that happened in one simulated session.

A log holds the map (so a replay needs nothing else), position frames, events
(instructions, blocked instructions, landings, go-arounds) and the scores. It is
saved as JSON and embedded in the radar replay.
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from approach.airports.model import Airport


@dataclass
class SessionLog:
    meta: dict[str, Any]
    map: dict[str, Any]
    frames: list[dict[str, Any]] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)
    scores: dict[str, Any] = field(default_factory=dict)
    # Real tracks drawn faintly behind the session's aircraft (real-traffic sessions): what really happened.
    ghosts: list[dict[str, Any]] = field(default_factory=list)

    def add_event(self, t: float, kind: str, callsign: str | None = None, text: str = "", **extra: Any) -> None:
        self.events.append({"t": round(t, 1), "kind": kind, "cs": callsign, "text": text, **extra})

    def to_json(self) -> str:
        return json.dumps(asdict(self), separators=(",", ":"))

    def save(self, path: Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.to_json())
        return path

    @classmethod
    def load(cls, path: Path) -> "SessionLog":
        return cls(**json.loads(Path(path).read_text()))


def map_for_replay(airport: Airport, runway: str, display_fixes: list[str]) -> dict[str, Any]:
    """Everything the radar replay draws, in local-grid nautical miles around the airport."""

    def xy(p):
        x, y = airport.xy(p)
        return [round(x, 3), round(y, 3)]

    rwy = airport.runways[runway]
    holds = []
    for h in airport.holds.values():
        holds.append(
            {
                "fix": h.fix,
                "xy": xy(h.position),
                "inbound_true": round(airport.magnetic_to_true(h.inbound_course_mag), 1),
                "turn": h.turn,
            }
        )
    westerly = rwy.course_true > 180
    airspace = {}
    for name, segs in airport.airspace.items():
        if ("Westerlies" in name and not westerly) or ("Easterlies" in name and westerly):
            continue
        airspace[name] = [[*xy(a), *xy(b)] for a, b in segs]
    return {
        "airport": airport.icao,
        "name": airport.name,
        "runway": runway,
        "runways": [
            {"name": r.name, "threshold": xy(r.threshold), "course_true": round(r.course_true, 2)}
            for r in airport.runways.values()
        ],
        "active": {
            "name": rwy.name,
            "threshold": xy(rwy.threshold),
            "course_true": round(rwy.course_true, 2),
        },
        "holds": holds,
        "fixes": [{"name": n, "xy": xy(airport.fixes[n])} for n in display_fixes if n in airport.fixes],
        "airspace": airspace,
        "variation_east": airport.magnetic_variation_east,
    }


def round_frame_value(v: float, nd: int = 2) -> float:
    return round(float(v), nd) if not math.isnan(v) else 0.0
