"""Real arrivals: every landing at the airport in a day of adsb.lol traffic.

For each arrival we find:

* the handover: the first moment it is within HANDOVER_RADIUS_NM of the airport
  and at or below HANDOVER_CEILING_FT, wherever it is (heading for a hold, on a
  direct or on a heading from the previous controller). This is where the AI
  takes over in the simulator, so it is also where real controllers are scored
  from;
* holding: laps flown near a hold fix, and for how long;
* go-arounds: down to low level on final, then climbing away without landing;
* the landing: which runway, and when it crossed the threshold;
* its track, from the edge of the area (50 NM, below FL200) to touchdown.

The result for one day is a small file in data/real/<airport>/arrivals/, which is
committed: it is derived from adsb.lol's data, so it is under the same licence
(ODbL 1.0) and credits adsb.lol.

    python -m approach.data.arrivals 2025-06-27
"""

from __future__ import annotations

import argparse
import gzip
import json
import math
from datetime import date
from pathlib import Path

from approach import wake
from approach.geo import heading_difference

HANDOVER_RADIUS_NM = 40.0
HANDOVER_CEILING_FT = 15000
REAL_DIR = Path(__file__).resolve().parents[2] / "data" / "real"
VISIT_GAP_S = 30 * 60  # a gap this long starts a new visit (the same airframe's next flight)
HOLD_RADIUS_NM = 6.0
MIN_STEP_S = 4.0  # keep at most one point every 4 s (the replay's frame interval)


def _alt(a) -> float | None:
    return 0.0 if a == "ground" else (float(a) if a is not None else None)


def _runway_frame(airport, rwy: str, lat: float, lon: float) -> tuple[float, float]:
    x, y = airport.grid.to_xy(lat, lon)
    return airport.runway_frame(rwy, x, y)


def _thinned(points: list[list]) -> list[list]:
    out = []
    for p in points:
        if not out or p[0] - out[-1][0] >= MIN_STEP_S:
            out.append(p)
    return out


def find_arrivals(area: dict, airport) -> list[dict]:
    """Every landing in a day's area traffic, with its handover, holding, go-arounds and track."""
    ref = airport.reference
    coslat = math.cos(math.radians(ref.lat))

    def dist(lat: float, lon: float) -> float:
        return math.hypot((lat - ref.lat) * 60.0, (lon - ref.lon) * 60.0 * coslat)

    holds = {name: (h.position.lat, h.position.lon) for name, h in airport.holds.items()}
    elev = max(r.elevation_ft for r in airport.runways.values())
    found = []
    for ac in area["aircraft"]:
        pts = sorted((p for p in ac["points"] if p[1] is not None), key=lambda p: p[0])
        visits, cur = [], []
        for p in pts:
            if cur and p[0] - cur[-1][0] > VISIT_GAP_S:
                visits.append(cur)
                cur = []
            cur.append(p)
        if cur:
            visits.append(cur)
        for v in visits:
            found += _arrivals_in_visit(ac, v, airport, dist, holds, elev)
    found.sort(key=lambda a: a["landed_t"])
    return found


def _arrivals_in_visit(ac: dict, pts: list[list], airport, dist, holds, elev: float) -> list[dict]:
    out = []
    start = 0
    while True:
        # Touchdown: on the ground (or rolling slowly) close to the airport, after being above 3,000 ft.
        i_land = None
        for i in range(start, len(pts)):
            p = pts[i]
            a = _alt(p[3])
            if a is None or dist(p[1], p[2]) > 2.5:
                continue
            on_ground = p[3] == "ground" or (p[4] is not None and p[4] < 80 and a <= elev + 600)
            if on_ground and any((_alt(q[3]) or 0) >= 3000 for q in pts[start:i]):
                i_land = i
                break
        if i_land is None:
            # No ground report (some days the receivers see little near the ground): accept a track that ends
            # lined up on a runway's final inside 4 NM, or over the runway itself, below 1,500 ft above the airfield.
            tail = next((q for q in reversed(pts[start:]) if q[3] != "ground"), None)
            a = _alt(tail[3]) if tail else None
            ends_on_final = a is not None and a <= elev + 1500 and any(
                -2.0 <= al <= 4.0 and abs(la) < 0.6
                for al, la in (_runway_frame(airport, r, tail[1], tail[2]) for r in airport.runways))
            if ends_on_final and any((_alt(q[3]) or 0) >= 3000 for q in pts[start:]):
                i_land = len(pts) - 1
            else:
                return out
        seg = pts[start:i_land + 1]
        arrival = _describe(ac, seg, airport, dist, holds, elev)
        if arrival:
            out.append(arrival)
        # Skip the landing roll and taxi before looking for another landing in the same visit.
        t_land = pts[i_land][0]
        start = next((k for k in range(i_land, len(pts)) if pts[k][0] - t_land > 20 * 60), len(pts))
        if start >= len(pts):
            return out


def _describe(ac: dict, seg: list[list], airport, dist, holds, elev: float) -> dict | None:
    # Runway: the one whose final course matches the track over the last few miles.
    airborne = [p for p in seg if p[3] != "ground" and p[5] is not None]
    if len(airborne) < 10:
        return None
    best, best_err = None, 999.0
    for name, r in airport.runways.items():
        near = [p for p in airborne if 1.0 <= _runway_frame(airport, name, p[1], p[2])[0] <= 6.0
                and abs(_runway_frame(airport, name, p[1], p[2])[1]) < 1.0]
        if len(near) >= 3:
            err = sum(abs(heading_difference(p[5], r.course_true)) for p in near) / len(near)
            if err < best_err:
                best, best_err = name, err
    if best is None or best_err > 20:
        return None
    rwy = airport.runways[best]

    # Threshold crossing, from the last airborne point before the threshold and its ground speed. Receivers often
    # lose aircraft in the last few feet and pick them up again minutes later on the taxiway, so interpolating
    # across that gap would put the landing far too late.
    landed_t = None
    last_before = None
    for p in seg:
        along, lat_ = _runway_frame(airport, best, p[1], p[2])
        if p[3] != "ground" and 0 < along <= 4.0 and abs(lat_) < 1.0:
            last_before = (p, along)
        elif last_before and along <= 0:
            q = last_before[0]
            gs = max(q[4] or 120.0, 60.0)
            landed_t = min(q[0] + last_before[1] / gs * 3600.0, p[0])
            break
    if landed_t is None:
        if last_before is None:
            return None
        q, along = last_before
        landed_t = q[0] + along / max(q[4] or 120.0, 60.0) * 3600.0

    # Handover: first point inside the approach area.
    k_hand = next((k for k, p in enumerate(seg) if dist(p[1], p[2]) <= HANDOVER_RADIUS_NM
                   and (_alt(p[3]) or 0) <= HANDOVER_CEILING_FT), None)
    if k_hand is None:
        return None
    h = seg[k_hand]
    cs = next((p[7] for p in reversed(seg) if p[7]), "") or (ac.get("reg") or ac.get("hex") or "").upper()

    # Go-arounds: down below 1,200 ft lined up on final, then climbing above 2,000 ft before landing.
    go_around_t, low_t = [], None
    for p in seg[k_hand:]:
        a = _alt(p[3])
        if a is None or p[3] == "ground":
            continue
        along, lat_ = _runway_frame(airport, best, p[1], p[2])
        if a <= elev + 1200 and 0 <= along <= 5 and abs(lat_) < 0.5:
            low_t = low_t or p[0]
        elif low_t and a >= elev + 2000:
            go_around_t.append(low_t)
            low_t = None

    # Holding: turning on the spot near a hold fix. A lap is 360 degrees of turn within HOLD_RADIUS_NM;
    # the fix with the most turning is the one it held at.
    hold_fix, best_turn, hold_span = None, 0.0, (None, None)
    for name, (hlat, hlon) in holds.items():
        near = [p for p in seg[k_hand:] if p[5] is not None and math.hypot(
            (p[1] - hlat) * 60, (p[2] - hlon) * 60 * math.cos(math.radians(hlat))) <= HOLD_RADIUS_NM]
        turn = sum(abs(heading_difference(b[5], a_[5])) for a_, b in zip(near, near[1:]))
        if turn >= 300 and turn > best_turn:
            hold_fix, best_turn, hold_span = name, turn, (near[0][0], near[-1][0])
    laps = int(best_turn // 360)
    hold_s = (hold_span[1] - hold_span[0]) if hold_fix else 0.0

    actype = (ac.get("type") or "").upper() or "A320"
    track = [[round(p[0], 1), p[1], p[2], p[3], p[4], p[5], p[6]] for p in _thinned(seg)]
    return {
        "callsign": cs,
        "hex": ac.get("hex"),
        "type": actype,
        "wake": wake.category(actype),
        "runway": best,
        "handover": {"t": h[0], "lat": h[1], "lon": h[2], "alt": _alt(h[3]), "gs": h[4], "track": h[5],
                     "vrate": h[6], "ias": h[9] if len(h) > 9 else None},
        "landed_t": round(landed_t, 1),
        "minutes_from_handover": round((landed_t - h[0]) / 60.0, 1),
        "hold_fix": hold_fix,
        "hold_minutes": round(hold_s / 60.0, 1),
        "hold_laps": laps,
        "hold_t": [round(hold_span[0], 1), round(hold_span[1], 1)] if hold_fix else None,
        "go_arounds": len(go_around_t),
        "go_around_t": [round(t, 1) for t in go_around_t],
        "track": track,  # t, lat, lon, alt (ft or "ground"), ground speed kt, track deg, vertical rate ft/min
    }


def arrivals_path(airport_icao: str, day: date) -> Path:
    return REAL_DIR / airport_icao.lower() / "arrivals" / f"{day.isoformat()}.json.gz"


def build_day(day: date, airport, log=print) -> dict:
    from approach.data import adsblol

    area = adsblol.get_day(day, airport, log=log)
    arrivals = find_arrivals(area, airport)
    out = {
        "day": day.isoformat(), "airport": airport.icao, "source": adsblol.CREDIT, "licence": "ODbL 1.0",
        "instance": area["instance"],
        "handover": {"radius_nm": HANDOVER_RADIUS_NM, "ceiling_ft": HANDOVER_CEILING_FT},
        "arrivals": arrivals,
    }
    path = arrivals_path(airport.icao, day)
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt") as f:
        json.dump(out, f, separators=(",", ":"))
    return out


def load_day(day: date, airport_icao: str = "EGKK") -> dict:
    with gzip.open(arrivals_path(airport_icao, day), "rt") as f:
        return json.load(f)


def main() -> None:
    ap = argparse.ArgumentParser(description="Find every real arrival in days of adsb.lol traffic")
    ap.add_argument("days", nargs="+")
    args = ap.parse_args()
    from approach.airports import egkk

    airport = egkk.load()
    for d in args.days:
        out = build_day(date.fromisoformat(d), airport)
        arr = out["arrivals"]
        rw = {}
        for a in arr:
            rw[a["runway"]] = rw.get(a["runway"], 0) + 1
        held = sum(1 for a in arr if a["hold_fix"])
        ga = sum(a["go_arounds"] for a in arr)
        print(f"{d}: {len(arr)} arrivals, runways {rw}, {held} held, {ga} go-arounds", flush=True)


if __name__ == "__main__":
    main()
