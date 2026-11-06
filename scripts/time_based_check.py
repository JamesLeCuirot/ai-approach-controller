"""Does spacing on final shrink with headwind since Gatwick moved to time-based separation?

With fixed distances, the distance between landings stays the same as the headwind grows and
the time between them goes up. With time-based separation it is the other way round: the time
stays about level and the distance shrinks. NATS moved Gatwick to time-based separation in
March 2025, so this compares the two periods.

For every pair of consecutive landings on the same runway (as approach.data.other_traffic.gaps):
the follower's distance behind the leader when the leader lands, the time between the landings,
the departures in between, and the reported surface headwind along the runway in that hour,
from Gatwick's weather reports. Download those first (Iowa State University archive):

    curl -o metar.csv "https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py?station=EGKK&data=sknt&data=drct&year1=2023&month1=12&day1=28&year2=2026&month2=10&day2=1&tz=Etc/UTC&format=onlycomma&missing=empty&report_type=3&report_type=4"

    python scripts/time_based_check.py metar.csv
"""

from __future__ import annotations

import bisect
import csv
import gzip
import json
import math
import statistics as st
import sys
from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from approach import wake  # noqa: E402
from approach.airports import egkk  # noqa: E402
from approach.data.arrivals import REAL_DIR, load_day  # noqa: E402

SWITCH = "2025-03-26"


def metar_wind(path: str) -> dict[tuple[str, int], list[tuple[float, float]]]:
    """(day, UTC hour) -> the reports in that hour, as (speed kt, direction true)."""
    out = defaultdict(list)
    for r in csv.DictReader(open(path)):
        if r["sknt"] and r["drct"]:
            out[(r["valid"][:10], int(r["valid"][11:13]))].append((float(r["sknt"]), float(r["drct"])))
    return out


def pairs(metar_path: str) -> list[dict]:
    airport = egkk.load()
    wind = metar_wind(metar_path)
    rows = []
    for path in sorted((REAL_DIR / "egkk" / "other").glob("*.json.gz")):
        day = date.fromisoformat(path.name[:10])
        other = json.load(gzip.open(path, "rt"))
        arrivals = sorted(load_day(day, "EGKK")["arrivals"], key=lambda a: a["landed_t"])
        deps = defaultdict(list)
        for o in other["aircraft"]:
            if o["kind"] == "gatwick_departure":
                deps[o["runway"]].append(o["takeoff_t"])
        for lead, follow in zip(arrivals, arrivals[1:]):
            rw = lead["runway"]
            if rw != follow["runway"] or follow["landed_t"] - lead["landed_t"] > 600:
                continue
            ts = [p[0] for p in follow["track"]]
            k = bisect.bisect_right(ts, lead["landed_t"])
            if k == 0 or k >= len(ts):
                continue
            p, q = follow["track"][k - 1], follow["track"][k]
            f = (lead["landed_t"] - p[0]) / (q[0] - p[0]) if q[0] > p[0] else 0.0
            x, y = airport.grid.to_xy(p[1] + (q[1] - p[1]) * f, p[2] + (q[2] - p[2]) * f)
            along, lateral = airport.runway_frame(rw, x, y)
            if not (0 < along < 15 and abs(lateral) < 0.5):
                continue
            t = datetime.fromtimestamp(lead["landed_t"], tz=timezone.utc)
            reports = wind.get((t.date().isoformat(), t.hour), [])
            if not reports:
                continue
            course = airport.runways[rw].course_true
            head = sum(s * math.cos(math.radians(d - course)) for s, d in reports) / len(reports)
            rows.append({"period": "after" if day.isoformat() >= SWITCH else "before",
                         "pair": wake.category(lead["type"], "UK") + wake.category(follow["type"], "UK"),
                         "departures": sum(1 for d in deps[rw] if lead["landed_t"] < d < follow["landed_t"]),
                         "nm": along, "s": follow["landed_t"] - lead["landed_t"], "headwind": head})
    return rows


def fit(g: list[dict], key: str) -> tuple[float, float]:
    """Change per 10 kt of headwind, and its 95% range."""
    xs = [r["headwind"] for r in g]
    ys = [r[key] for r in g]
    mx, my = st.mean(xs), st.mean(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    b = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
    res = sum((y - my - b * (x - mx)) ** 2 for x, y in zip(xs, ys))
    return b * 10, 1.96 * math.sqrt(res / (len(xs) - 2) / sxx) * 10


def main() -> None:
    rows = [r for r in pairs(sys.argv[1]) if -10 <= r["headwind"] <= 30]
    print("Per 10 kt of headwind. Fixed distances: distance flat, time up. Time-based: time flat, distance down.\n")
    groups = [
        ("No departure in between, lower medium behind lower medium", lambda r: r["departures"] == 0 and r["pair"] == "MM"),
        ("No departure in between, behind a heavy or super", lambda r: r["departures"] == 0 and r["pair"][0] in "JH"),
        ("One departure in between", lambda r: r["departures"] == 1),
        ("Two departures in between", lambda r: r["departures"] == 2),
    ]
    for label, keep in groups:
        print(label)
        for period in ("before", "after"):
            g = [r for r in rows if r["period"] == period and keep(r)]
            dn, en = fit(g, "nm")
            ds, es = fit(g, "s")
            print(f"  {period:6} {SWITCH}: {len(g):5} pairs, median {st.median(r['nm'] for r in g):.2f} NM;"
                  f" distance {dn:+.2f} ±{en:.2f} NM, time {ds:+.1f} ±{es:.1f} s")


if __name__ == "__main__":
    main()
