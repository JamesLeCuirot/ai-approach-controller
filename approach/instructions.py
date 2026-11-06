"""Controller instructions and how they are read out on the frequency."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class Instruction:
    callsign: str
    kind: str  # "heading", "altitude", "speed", "cleared_ils", "direct", "hold", "resume_speed"
    value: Any = None  # true heading (deg), altitude (ft), speed (kt), runway or fix name

    def phrase(self, current_heading: float | None = None, current_alt: float | None = None,
               variation_east: float = 0.0, transition_altitude: int = 6000) -> str:
        """Radio phraseology for the transcript (headings are given magnetic)."""
        cs = self.callsign
        if self.kind == "heading":
            mag = round((float(self.value) - variation_east) % 360) or 360
            turn = ""
            if current_heading is not None:
                diff = (float(self.value) - current_heading + 180) % 360 - 180
                turn = "turn right " if diff > 0 else "turn left "
            return f"{cs}, {turn}heading {mag:03d}"
        if self.kind == "altitude":
            alt = int(self.value)
            verb = "descend" if current_alt is None or alt < current_alt else "climb"
            level = f"altitude {alt:,} ft" if alt <= transition_altitude else f"flight level {alt // 100}"
            return f"{cs}, {verb} {level}"
        if self.kind == "speed":
            return f"{cs}, speed {int(self.value)} knots"
        if self.kind == "resume_speed":
            return f"{cs}, resume normal speed"
        if self.kind == "cleared_ils":
            return f"{cs}, cleared ILS approach runway {self.value}"
        if self.kind == "direct":
            return f"{cs}, proceed direct {self.value}"
        if self.kind == "hold":
            return f"{cs}, hold at {self.value}"
        return f"{cs}, {self.kind} {self.value}"
