"""Run a scenario and write its session log and radar replay.

    python -m approach.run two_arrivals
    python -m approach.run two_arrivals --weather 2024-12-07T09
    python -m approach.run --list
"""

from __future__ import annotations

import argparse
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from approach.airports import egkk
from approach.radar import replay
from approach.scenarios import SCENARIOS, Scenario
from approach.session import SessionLog, map_for_replay

ROOT = Path(__file__).resolve().parents[1]


def code_version() -> str:
    try:
        rev = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True, text=True).stdout.strip()
        dirty = subprocess.run(["git", "status", "--porcelain", "--", "approach"], cwd=ROOT, capture_output=True, text=True).stdout.strip()
        return rev + ("+changes" if dirty else "") if rev else "unknown"
    except OSError:
        return "unknown"


WIND_HEIGHTS_FT = (1000, 2000, 3000, 5000, 8000)


def weather_summary(wind, airport) -> dict:
    """The wind at the airport at a few heights, for the replay and the diary."""
    ref = airport.reference
    rows = []
    for h in (wind.heights_ft[0],) + WIND_HEIGHTS_FT:
        d, s = wind.wind(ref.lat, ref.lon, h)
        rows.append({"alt": "surface" if h == wind.heights_ft[0] else h, "dir": round(d) % 360 or 360, "kt": round(s)})
    return {"valid": wind.valid, "source": wind.source, "levels": rows}


def run_scenario(scenario: Scenario, controller: str | None = None, seed: int = 1, milestone: str = "",
                 weather: str | None = None) -> SessionLog:
    airport = egkk.load()
    controller = controller or scenario.controller
    weather = weather or scenario.weather
    wind = None
    if weather:
        from approach.data.gfs import fetch_for_airport, parse_time

        wind = fetch_for_airport(parse_time(weather), airport)
    log = SessionLog(
        meta={
            "title": scenario.title,
            "scenario": scenario.name,
            "controller": controller,
            "runway": scenario.runway,
            "duration": scenario.duration_s,
            "milestone": milestone,
            "code_version": code_version(),
            "generated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
            "notes": scenario.notes,
            "seed": seed,
            **({"weather": weather_summary(wind, airport)} if wind else {}),
        },
        map=map_for_replay(airport, scenario.runway, egkk.DISPLAY_FIXES),
    )
    if not scenario.arrivals:
        log.frames.append({"t": 0.0, "ac": []})
        return log

    from approach.sim.session_runner import run_session  # BlueSky is only needed when there is traffic

    return run_session(airport, scenario, controller, log, seed=seed, wind=wind)


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("scenario", nargs="?", help="scenario name")
    p.add_argument("--controller", help="override the scenario's controller (rules, random, script, none)")
    p.add_argument("--out", default="outputs", help="output folder")
    p.add_argument("--seed", type=int, default=1)
    p.add_argument("--weather", help="fly in the real winds of this UTC hour, e.g. 2024-12-07T09 (NOAA GFS)")
    p.add_argument("--list", action="store_true", help="list scenarios")
    args = p.parse_args(argv)
    if args.list or not args.scenario:
        for s in SCENARIOS.values():
            print(f"{s.name:22} {s.title}")
        return
    log = run_scenario(SCENARIOS[args.scenario], args.controller, args.seed, weather=args.weather)
    out = Path(args.out)
    log.save(out / f"{args.scenario}.json")
    path = replay.write(log, out / f"{args.scenario}.html")
    print(f"Replay: {path}")
    if log.scores:
        for k, v in log.scores.items():
            print(f"  {k}: {v}")


if __name__ == "__main__":
    main()
