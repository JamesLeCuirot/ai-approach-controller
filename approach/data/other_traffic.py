"""Everything near Gatwick that isn't a Gatwick arrival: Gatwick's departures, Heathrow's traffic, overflights.

From a day's full area file (approach.data.adsblol) we keep, for every aircraft that is not
a Gatwick arrival, its points within OTHER_RADIUS_NM of Gatwick and at or below
OTHER_CEILING_FT, one every 8 seconds. Gatwick departures are marked with their take-off
time and runway. The result is a small file per day in data/real/<airport>/other/
(committed, ODbL like the arrivals), so the full 3.5 GB days are not needed again.
Departures keep a point every 8 s with ground speed and track. The rest keep a point every
15 s with position and altitude only, and only within 10 NM of either runway's RMA: our
arrivals can't leave the RMA, so traffic further away (most of Heathrow's) can't meet them.

It also answers the spacing question: for each pair of landings, how many departures
went between them, and how far apart the two arrivals were.

    python -m approach.data.other_traffic 2025-06-27
    python -m approach.data.other_traffic --gaps          (summary over every day saved)
"""

from __future__ import annotations

import argparse
import bisect
import csv
import gzip
import json
import math
from collections import defaultdict
from datetime import date
from pathlib import Path

from approach.data.arrivals import REAL_DIR, arrivals_path, load_day
from approach.geo import heading_difference

OTHER_RADIUS_NM = 40.0
OTHER_CEILING_FT = 15000
STEP_S = 8.0  # Gatwick departures: they climb out through the arrivals, so they keep more detail
OTHER_STEP_S = 15.0  # everything else: position and altitude only
NEAR_RMA_NM = 10.0  # everything else is kept only near the RMAs, where our arrivals can be
DEPARTURE_RADIUS_NM = 3.0


def other_path(icao: str, day: date) -> Path:
    return REAL_DIR / icao.lower() / "other" / f"{day.isoformat()}.json.gz"


def _alt(a) -> float | None:
    return 0.0 if a == "ground" else (float(a) if a is not None else None)


def build_day(day: date, airport) -> dict:
    from approach.data import adsblol

    area = adsblol.get_day(day, airport)
    arrivals = load_day(day, airport.icao)["arrivals"] if arrivals_path(airport.icao, day).is_file() else []
    arrival_windows = defaultdict(list)  # hex -> [(from, to)] times that belong to a Gatwick arrival
    for a in arrivals:
        arrival_windows[a["hex"]].append((a["track"][0][0] - 60, a["landed_t"] + 600))
    ref = airport.reference
    coslat = math.cos(math.radians(ref.lat))
    elev = max(r.elevation_ft for r in airport.runways.values())

    def dist(lat, lon):
        return math.hypot((lat - ref.lat) * 60.0, (lon - ref.lon) * 60.0 * coslat)

    from shapely import contains_xy
    from shapely.ops import unary_union

    near = unary_union([airport.rma(r).polygon for r in airport.runways if airport.rma(r) is not None]).buffer(NEAR_RMA_NM)

    out = []
    for ac in area["aircraft"]:
        wins = arrival_windows.get(ac["hex"], [])
        pts = sorted((p for p in ac["points"] if p[1] is not None
                      and not any(lo <= p[0] <= hi for lo, hi in wins)), key=lambda p: p[0])
        pts = [p for p in pts if dist(p[1], p[2]) <= OTHER_RADIUS_NM and (_alt(p[3]) or 0) <= OTHER_CEILING_FT]
        if len(pts) < 3:
            continue
        # Split into visits, then keep one point every STEP_S seconds.
        visits, cur = [], []
        for p in pts:
            if cur and p[0] - cur[-1][0] > 900:
                visits.append(cur)
                cur = []
            cur.append(p)
        visits.append(cur)
        for v in visits:
            dep = _departure(v, airport, dist, elev)
            if not dep:
                xy = [airport.grid.to_xy(p[1], p[2]) for p in v]
                v = [p for p, (x, y) in zip(v, xy) if contains_xy(near, x, y)]
                if len(v) < 3:
                    continue
            step = STEP_S if dep else OTHER_STEP_S
            thin = []
            for p in v:
                if not thin or p[0] - thin[-1][0] >= step:
                    thin.append(p)
            if dep:
                track = [[round(p[0]), round(p[1], 4), round(p[2], 4), p[3], p[4], p[5]] for p in thin]
            else:
                track = [[round(p[0]), round(p[1], 4), round(p[2], 4), p[3]] for p in thin]
            entry = {"hex": ac["hex"], "type": (ac.get("type") or "").upper(),
                     "callsign": next((p[7] for p in v if p[7]), ""), "kind": "other", "track": track}
            if dep:
                entry["kind"] = "gatwick_departure"
                entry.update(dep)
            if len(thin) >= 3:
                out.append(entry)
    result = {"day": day.isoformat(), "airport": airport.icao, "source": area.get("source"), "licence": "ODbL 1.0",
              "radius_nm": OTHER_RADIUS_NM, "ceiling_ft": OTHER_CEILING_FT,
              "fields": {"gatwick_departure": ["t", "lat", "lon", "alt", "gs", "track"], "other": ["t", "lat", "lon", "alt"]},
              "aircraft": out}
    path = other_path(airport.icao, day)
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt") as f:
        json.dump(result, f, separators=(",", ":"))
    return result


def _departure(v: list[list], airport, dist, elev: float) -> dict | None:
    """A take-off from Gatwick: low over the runway, then climbing through 3,000 ft within ten minutes."""
    for i, p in enumerate(v):
        a = _alt(p[3])
        if a is None or dist(p[1], p[2]) > DEPARTURE_RADIUS_NM or a > elev + 1500:
            continue
        later = [q for q in v[i:] if q[0] - p[0] <= 600]
        if not any((_alt(q[3]) or 0) >= 3000 for q in later):
            continue
        if any((_alt(q[3]) or 0) >= 4000 for q in v[:i] if p[0] - q[0] <= 900):
            return None  # it came down from above: an arrival or a go-around, not a take-off
        climbing = [q for q in later if q[3] != "ground" and q[5] is not None and (_alt(q[3]) or 0) < elev + 2500]
        if not climbing:
            return None
        trk = climbing[0][5]
        runway = min(airport.runways, key=lambda r: abs(heading_difference(trk, airport.runways[r].course_true)))
        airborne = next((q for q in v[i:] if q[3] != "ground" and (_alt(q[3]) or 0) >= elev + 200), climbing[0])
        return {"takeoff_t": round(airborne[0], 1), "runway": runway}
    return None


def gaps(icao: str = "EGKK") -> list[dict]:
    """For each pair of landings on the same runway within ten minutes: departures between them and the spacing."""
    from approach.airports import egkk

    airport = egkk.load()
    rows = []
    for path in sorted((REAL_DIR / icao.lower() / "other").glob("*.json.gz")):
        day = date.fromisoformat(path.name[:10])
        with gzip.open(path, "rt") as f:
            other = json.load(f)
        arrivals = sorted(load_day(day, icao)["arrivals"], key=lambda a: a["landed_t"])
        deps = defaultdict(list)
        for o in other["aircraft"]:
            if o["kind"] == "gatwick_departure":
                deps[o["runway"]].append(o["takeoff_t"])
        for r in deps:
            deps[r].sort()
        for lead, follow in zip(arrivals, arrivals[1:]):
            if lead["runway"] != follow["runway"] or follow["landed_t"] - lead["landed_t"] > 600:
                continue
            ts = [p[0] for p in follow["track"]]
            k = bisect.bisect_right(ts, lead["landed_t"])
            if k == 0 or k >= len(ts):
                continue
            p, q = follow["track"][k - 1], follow["track"][k]
            f = (lead["landed_t"] - p[0]) / (q[0] - p[0]) if q[0] > p[0] else 0.0
            x, y = airport.grid.to_xy(p[1] + (q[1] - p[1]) * f, p[2] + (q[2] - p[2]) * f)
            along, lateral = airport.runway_frame(follow["runway"], x, y)
            if not (0 < along < 15 and abs(lateral) < 0.5):
                continue
            between = [d for d in deps[lead["runway"]] if lead["landed_t"] < d < follow["landed_t"]]
            rows.append({"day": day.isoformat(), "runway": lead["runway"], "leader": lead["type"],
                         "follower": follow["type"], "departures_between": len(between),
                         "spacing_nm": round(along, 2), "seconds": round(follow["landed_t"] - lead["landed_t"], 1)})
    return rows


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("days", nargs="*")
    ap.add_argument("--gaps", action="store_true", help="summarise landing gaps against departures between them")
    args = ap.parse_args()
    from approach.airports import egkk

    airport = egkk.load()
    for d in args.days:
        out = build_day(date.fromisoformat(d), airport)
        kinds = defaultdict(int)
        for a in out["aircraft"]:
            kinds[a["kind"]] += 1
        print(f"{d}: {dict(kinds)}", flush=True)
    if args.gaps:
        rows = gaps(airport.icao)
        path = REAL_DIR / airport.icao.lower() / "landing_gaps.csv"
        with path.open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
        by = defaultdict(list)
        for r in rows:
            by[min(r["departures_between"], 3)].append(r)
        print(f"Wrote {path}: {len(rows)} pairs of landings")
        for n in sorted(by):
            sp = sorted(r["spacing_nm"] for r in by[n])
            sec = sorted(r["seconds"] for r in by[n])
            q = lambda xs, f: xs[min(len(xs) - 1, int(f * len(xs)))]  # noqa: E731
            print(f"  {n}{'+' if n == 3 else ''} departures between: {len(sp)} pairs, spacing median {q(sp, .5):.1f} NM "
                  f"(middle half {q(sp, .25):.1f}-{q(sp, .75):.1f}), {q(sec, .5):.0f} s apart")


if __name__ == "__main__":
    main()
