"""Real hours of traffic as sessions: real controllers scored, and the same traffic for our controllers.

A window of a real day (say 15:00-16:00 UTC) gives:

* ``real_session``: what the real controllers did, as a session log with the same
  frames and events as a simulated session, so ``approach.scoring.score`` measures
  them exactly as it measures our controllers, and the radar replay can show it;
* ``real_scenario``: the same arrivals released into the simulator at their real
  handover positions, levels, speeds and tracks, in the real winds of that hour;
* ``ghosts``: the real tracks, for the radar replay to draw faintly behind our
  controller's aircraft.

Both start empty: only arrivals handed over inside the window take part, so the
real controllers and ours face exactly the same traffic.
"""

from __future__ import annotations

import bisect
import math
from datetime import datetime, timezone

from approach import scoring, wake
from approach.airports.model import Airport
from approach.geo import heading_difference
from approach.scenarios import Arrival, Scenario
from approach.session import SessionLog, map_for_replay

FRAME_S = 4.0
FINAL_LATERAL_NM = 0.3
GO_AROUND_SHOW_S = 180.0
# Real controllers' instructions are not in the data, so these scores do not apply to them.
NOT_MEASURED = ("instructions", "instructions_per_arrival", "safety_blocks", "stable_at_1000ft_pct")


def window_arrivals(day: dict, start: float, end: float) -> list[dict]:
    return [a for a in day["arrivals"] if start <= a["handover"]["t"] < end]


def runway_in_use(arrivals: list[dict]) -> str:
    counts: dict[str, int] = {}
    for a in arrivals:
        counts[a["runway"]] = counts.get(a["runway"], 0) + 1
    return max(counts, key=counts.get) if counts else "26L"


def _interp(track: list[list], t: float, elev: float):
    ts = [p[0] for p in track]
    k = bisect.bisect_right(ts, t)
    if k == 0 or k >= len(track):
        return None
    p, q = track[k - 1], track[k]
    f = (t - p[0]) / (q[0] - p[0]) if q[0] > p[0] else 0.0
    alt = lambda a: elev if a == "ground" or a is None else a  # noqa: E731
    lat = p[1] + (q[1] - p[1]) * f
    lon = p[2] + (q[2] - p[2]) * f
    gs = (p[4] or q[4] or 0) if p[4] is None or q[4] is None else p[4] + (q[4] - p[4]) * f
    trk = q[5] if p[5] is None else (p[5] + heading_difference(q[5], p[5]) * f) % 360 if q[5] is not None else p[5]
    return lat, lon, alt(p[3]) + (alt(q[3]) - alt(p[3])) * f, gs, trk or 0.0, p[6] or 0


def _phase(a: dict, t: float, along: float, lateral: float, trk: float, course: float) -> str:
    if any(g <= t < g + GO_AROUND_SHOW_S for g in a.get("go_around_t", [])):
        return "go_around"
    if a.get("hold_t") and a["hold_t"][0] <= t <= a["hold_t"][1]:
        return "holding"
    if 0 < along < 25 and abs(lateral) < FINAL_LATERAL_NM and abs(heading_difference(trk, course)) < 20:
        return "established"
    return "vectoring"


def _utc(t: float) -> str:
    return datetime.fromtimestamp(t, tz=timezone.utc).strftime("%H:%M")


def _describe_handover(a: dict, airport: Airport) -> str:
    h = a["handover"]
    x, y = airport.grid.to_xy(h["lat"], h["lon"])
    d = math.hypot(x, y)
    brg = (math.degrees(math.atan2(x, y)) + 360) % 360
    side = "N NE E SE S SW W NW".split()[int(((brg + 22.5) % 360) // 45)]
    lvl = f"FL{round(h['alt'] / 100):03d}" if h["alt"] > airport.transition_altitude_ft else f"{h['alt']:,.0f} ft"
    return f"{a['callsign']} ({a['type']}) handed over {d:.0f} NM {side}, {lvl}, {h['gs']:.0f} kt over the ground"


def real_session(day: dict, airport: Airport, start: float, end: float, title: str = "") -> SessionLog:
    arrivals = window_arrivals(day, start, end)
    runway = runway_in_use(arrivals)
    rwy = airport.runways[runway]
    elev = rwy.elevation_ft
    t_end = max((a["landed_t"] for a in arrivals), default=end) + 60
    log = SessionLog(
        meta={
            "title": title or f"Real controllers: Gatwick {day['day']}, {_utc(start)}-{_utc(end)} UTC",
            "scenario": f"real_{day['day']}_{_utc(start).replace(':', '')}",
            "controller": "real",
            "runway": runway,
            "duration": t_end - start,
            "t0_epoch": start,
            "source": day["source"],
            "notes": "Real ADS-B tracks. The real controllers' instructions are not known, so instruction counts "
                     "are not measured.",
        },
        map=map_for_replay(airport, runway, _display_fixes()),
    )
    for a in arrivals:
        log.add_event(a["handover"]["t"] - start, "takeover", a["callsign"], _describe_handover(a, airport))
        for g in a.get("go_around_t", []):
            log.add_event(g - start, "go_around", a["callsign"], f"{a['callsign']} went around")
        if a.get("hold_t"):
            log.add_event(a["hold_t"][0] - start, "note", a["callsign"],
                          f"{a['callsign']} holding at {a['hold_fix']} ({a['hold_minutes']:.0f} min)")
        if a["runway"] == runway:
            log.add_event(a["landed_t"] - start, "landed", a["callsign"],
                          f"{a['callsign']} landed runway {runway}", actype=a["type"])
        else:
            log.add_event(a["landed_t"] - start, "note", a["callsign"], f"{a['callsign']} landed runway {a['runway']}")
    log.events.sort(key=lambda e: e["t"])

    t = start
    while t <= t_end:
        ac = []
        for a in arrivals:
            if not (a["handover"]["t"] <= t <= a["landed_t"]):
                continue
            s = _interp(a["track"], t, elev)
            if s is None:
                continue
            lat, lon, alt, gs, trk, vs = s
            x, y = airport.grid.to_xy(lat, lon)
            along, lateral = airport.runway_frame(a["runway"], x, y)
            ac.append({"cs": a["callsign"], "type": a["type"], "wake": a["wake"], "x": round(x, 3), "y": round(y, 3),
                       "alt": round(alt), "cas": round(gs), "gs": round(gs), "hdg": round(trk, 1), "vs": round(vs),
                       "phase": _phase(a, t, along, lateral, trk, airport.runways[a["runway"]].course_true)})
        log.frames.append({"t": round(t - start, 1), "ac": ac})
        t += FRAME_S

    scores = scoring.score(log, airport, runway)
    for k in NOT_MEASURED:
        scores[k] = ""
    scores["holding_minutes_per_arrival"] = round(sum(a["hold_minutes"] for a in arrivals) / len(arrivals), 1) if arrivals else ""
    log.scores = scores
    log.meta["scores_labels"] = {**scoring.LABELS, "holding_minutes_per_arrival": "Holding minutes per arrival",
                                 "minutes_per_arrival": "Minutes from handover to landing"}
    return log


def ghosts(day: dict, airport: Airport, start: float, end: float) -> list[dict]:
    """The real tracks of the window's arrivals, from the edge of the area to touchdown, in replay time."""
    out = []
    for a in window_arrivals(day, start, end):
        pts = []
        for p in a["track"]:
            x, y = airport.grid.to_xy(p[1], p[2])
            alt = airport.runways[a["runway"]].elevation_ft if p[3] == "ground" else (p[3] or 0)
            pts.append([round(p[0] - start, 1), round(x, 3), round(y, 3), round(alt)])
        out.append({"cs": a["callsign"], "type": a["type"], "handover_t": round(a["handover"]["t"] - start, 1),
                    "landed_t": round(a["landed_t"] - start, 1), "points": pts})
    return out


def real_scenario(day: dict, airport: Airport, start: float, end: float, name: str = "", wind: bool = True) -> Scenario:
    """The window's real arrivals, released into the simulator at their real handover state."""
    arrivals = window_arrivals(day, start, end)
    runway = runway_in_use(arrivals)
    out = []
    for a in arrivals:
        h = a["handover"]
        nearest = min(airport.holds.values(), key=lambda hold: math.hypot(
            *(u - v for u, v in zip(airport.xy(hold.position), airport.grid.to_xy(h["lat"], h["lon"])))))
        out.append(Arrival(
            callsign=a["callsign"], actype=a["type"], fix=nearest.fix, release_s=round(h["t"] - start, 1),
            level_ft=int(round(h["alt"] / 100.0) * 100), speed_kt=0,
            lat=h["lat"], lon=h["lon"], track=h["track"], gs_kt=h["gs"],
        ))
    last = max((a["landed_t"] for a in arrivals), default=end)
    hour = datetime.fromtimestamp(start + 1800, tz=timezone.utc).strftime("%Y-%m-%dT%H")
    return Scenario(
        name=name or f"real_{day['day']}_{_utc(start).replace(':', '')}",
        title=f"Real traffic: Gatwick {day['day']}, {_utc(start)}-{_utc(end)} UTC",
        runway=runway,
        arrivals=out,
        duration_s=round(last - start + 1200),
        weather=hour if wind else None,
        notes=f"{len(out)} real arrivals handed over at their real positions. Traffic: {day['source']}.",
    )


def _display_fixes() -> list[str]:
    from approach.airports import egkk

    return egkk.DISPLAY_FIXES


def parse_hour(text: str) -> float:
    """'2025-06-27T15' -> that hour's start, in seconds since 1970 (UTC)."""
    t = text.strip().rstrip("Z")
    if len(t) == 13:
        t += ":00"
    return datetime.fromisoformat(t).replace(tzinfo=timezone.utc).timestamp()


def session_log(spec: str, milestone: str = "", hours: float = 1.0) -> SessionLog:
    """A session for one real hour: 'real:2025-06-27T15' (the real controllers) or 'rules:2025-06-27T15'
    (our rules controller on the same traffic, in the same winds, with the real tracks as ghosts)."""
    from datetime import date as _date

    from approach.airports import egkk
    from approach.data.arrivals import arrivals_path, build_day, load_day

    kind, hour = spec.split(":", 1)
    airport = egkk.load()
    start = parse_hour(hour)
    day_str = datetime.fromtimestamp(start, tz=timezone.utc).date().isoformat()
    if not arrivals_path(airport.icao, _date.fromisoformat(day_str)).is_file():
        build_day(_date.fromisoformat(day_str), airport)
    day = load_day(_date.fromisoformat(day_str), airport.icao)
    end = start + hours * 3600
    if kind == "real":
        log = real_session(day, airport, start, end)
    else:
        from approach.run import run_scenario

        scenario = real_scenario(day, airport, start, end, name=f"{kind}_{day_str}_{_utc(start).replace(':', '')}")
        log = run_scenario(scenario, controller=kind, milestone=milestone)
        log.meta["title"] = f"{kind.capitalize()} controller on real traffic: Gatwick {day_str}, {_utc(start)}-{_utc(end)} UTC"
        log.meta["t0_epoch"] = start
        log.meta["source"] = day["source"]
        log.ghosts = ghosts(day, airport, start, end)
    log.meta["milestone"] = milestone
    log.meta["scenario"] = spec.replace(":", "_")
    return log


def main() -> None:
    import argparse
    from pathlib import Path

    from approach.radar import replay

    ap = argparse.ArgumentParser(description="Score real controllers on a real hour, and run our controller on it")
    ap.add_argument("hour", help="UTC hour, e.g. 2025-06-27T15")
    ap.add_argument("--controller", default="rules", help="our controller to run on the same traffic (or 'none')")
    ap.add_argument("--hours", type=float, default=1.0)
    ap.add_argument("--out", default="outputs")
    args = ap.parse_args()
    kinds = ["real"] + ([args.controller] if args.controller != "none" else [])
    for kind in kinds:
        log = session_log(f"{kind}:{args.hour}", hours=args.hours)
        name = f"{kind}_{args.hour.replace(':', '')}"
        log.save(Path(args.out) / f"{name}.json")
        path = replay.write(log, Path(args.out) / f"{name}.html")
        s = log.scores
        print(f"{kind:6} {path}  landed {s.get('landed')}/{s.get('arrivals')}  losses {s.get('separation_losses')}  "
              f"closest {s.get('min_spacing_nm')} NM  mean {s.get('mean_spacing_nm')} NM  "
              f"track miles {s.get('track_miles_per_arrival')}  minutes {s.get('minutes_per_arrival')}  "
              f"go-arounds {s.get('go_arounds')}", flush=True)


if __name__ == "__main__":
    main()
