"""Score a session log with the plan's measures (the same for every controller)."""

from __future__ import annotations

import math
from collections import defaultdict

from approach import wake
from approach.airports.model import Airport
from approach.session import SessionLog

LABELS = {
    "arrivals": "Arrivals",
    "landed": "Landed",
    "separation_losses": "Separation losses",
    "wake_violations": "Wake spacing below minimum",
    "min_spacing_nm": "Closest spacing at runway (NM)",
    "mean_spacing_nm": "Average spacing at runway (NM)",
    "track_miles_per_arrival": "Track miles per arrival",
    "minutes_per_arrival": "Minutes from release to landing",
    "instructions": "Instructions",
    "instructions_per_arrival": "Instructions per arrival",
    "safety_blocks": "Blocked by safety check",
    "go_arounds": "Go-arounds",
    "stable_at_1000ft_pct": "Stable at 1,000 ft (%)",
}


def score(log: SessionLog, airport: Airport, runway: str) -> dict:
    events, frames = log.events, log.frames
    kinds = defaultdict(list)
    for e in events:
        kinds[e["kind"]].append(e)

    # Separation losses: episodes where a pair is within 3 NM and 1,000 ft.
    in_loss: set[tuple[str, str]] = set()
    losses = 0
    new_events = []
    for f in frames:
        ac = f["ac"]
        now = set()
        for i in range(len(ac)):
            for j in range(i + 1, len(ac)):
                a, b = ac[i], ac[j]
                h = math.hypot(a["x"] - b["x"], a["y"] - b["y"])
                v = abs(a["alt"] - b["alt"])
                if h < 3.0 and v < 1000:
                    pair = tuple(sorted((a["cs"], b["cs"])))
                    now.add(pair)
                    if pair not in in_loss:
                        losses += 1
                        new_events.append({"t": f["t"], "kind": "separation_loss", "cs": pair[0],
                                           "text": f"Separation lost: {pair[0]} and {pair[1]}, {h:.1f} NM and {v:,.0f} ft"})
        in_loss = now

    # Spacing at the runway: where each follower is on final when its leader lands. A follower that is not
    # yet established on final (still being vectored, or going around) is not counted.
    landings = sorted(kinds["landed"], key=lambda e: e["t"])
    spacings, wake_violations = [], 0
    for lead, follow in zip(landings, landings[1:]):
        frame = max((f for f in frames if f["t"] <= lead["t"]), key=lambda f: f["t"], default=None)
        pos = next((a for a in frame["ac"] if a["cs"] == follow["cs"]), None) if frame else None
        if pos is None or pos.get("phase", "established") != "established":
            continue
        along, _ = airport.runway_frame(runway, pos["x"], pos["y"])
        spacings.append(along)
        if along < wake.separation_nm(lead["actype"], follow["actype"]) - 0.05:
            wake_violations += 1

    # Track miles and time from release to landing.
    last: dict[str, tuple[float, float]] = {}
    miles: dict[str, float] = defaultdict(float)
    for f in frames:
        for a in f["ac"]:
            if a["cs"] in last:
                px, py = last[a["cs"]]
                miles[a["cs"]] += math.hypot(a["x"] - px, a["y"] - py)
            last[a["cs"]] = (a["x"], a["y"])
    released = {e["cs"]: e["t"] for e in kinds["takeover"]}
    landed_cs = [e["cs"] for e in landings]
    track = [miles[c] for c in landed_cs]
    minutes = [(e["t"] - released[e["cs"]]) / 60 for e in landings if e["cs"] in released]
    checks = kinds["stable_check"]
    arrivals = len(kinds["takeover"])

    log.events = sorted(events + new_events, key=lambda e: e["t"])
    return {
        "arrivals": arrivals,
        "landed": len(landings),
        "separation_losses": losses,
        "wake_violations": wake_violations,
        "min_spacing_nm": round(min(spacings), 1) if spacings else "",
        "mean_spacing_nm": round(sum(spacings) / len(spacings), 1) if spacings else "",
        "track_miles_per_arrival": round(sum(track) / len(track), 1) if track else "",
        "minutes_per_arrival": round(sum(minutes) / len(minutes), 1) if minutes else "",
        "instructions": len(kinds["instruction"]),
        "instructions_per_arrival": round(len(kinds["instruction"]) / arrivals, 1) if arrivals else "",
        "safety_blocks": len(kinds["blocked"]),
        "go_arounds": len(kinds["go_around"]),
        "stable_at_1000ft_pct": round(100 * sum(c["stable"] for c in checks) / len(checks)) if checks else "",
    }
