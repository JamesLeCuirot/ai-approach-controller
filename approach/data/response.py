"""How real aircraft respond to instructions, measured from the real arrivals.

The ADS-B data has no instructions in it, but it shows what aircraft do once they have one:
how fast they turn, how fast they descend and level off, and how fast they slow down. Those
are what the simulated pilots and the model of how aircraft respond need (stage 2).

* Turns: the rate of turn through the air (the heading, worked out from the track and the GFS
  wind), and the bank angle it implies at that true airspeed, for every turn of 10 degrees or more.
* Descents: the rate while descending between 11,000 and 3,500 ft, and how far above its new
  level an aircraft starts to level off.
* Speed changes: how fast the indicated airspeed (from the ground speed, the wind and the
  altitude) falls or rises, level and descending.

Only the training days are measured, so the test days stay unseen for checking the model.
Every track is first put on an even 5 s grid (gaps over 15 s split it).

    python -m approach.data.response        # writes data/real/egkk/response.json
"""

from __future__ import annotations

import argparse
import csv
import json
import math
from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path

import numpy as np

from approach.data.arrivals import REAL_DIR, load_day
from approach.data.gfs import CACHE_DIR, WindProfile

STEP_S = 5.0
MAX_GAP_S = 15.0
G_KT_DEG = 1092.6  # rate of turn (deg/s) = G_KT_DEG * tan(bank) / true airspeed (kt)

TURN_MIN_RATE = 0.6  # deg/s: slower than this is not a turn
TURN_MIN_DEG = 10.0
DESCENT_MIN_FPM = 300.0
DESCENT_BAND_FT = (3500.0, 11000.0)
SPEED_MIN_RATE = 0.2  # kt/s
SPEED_MIN_CHANGE = 8.0  # kt


def cas_from_tas(tas_kt: np.ndarray, alt_ft: np.ndarray) -> np.ndarray:
    """Indicated (calibrated) airspeed from true airspeed, in the standard atmosphere. Below 11,000 ft the
    difference between equivalent and calibrated airspeed is under 2 kt, so the density ratio is enough."""
    sigma = np.clip(1.0 - 6.8756e-6 * np.asarray(alt_ft, dtype=float), 0.2, 1.2) ** 4.2559
    return tas_kt * np.sqrt(sigma)


def tas_from_cas(cas_kt: np.ndarray, alt_ft: np.ndarray) -> np.ndarray:
    sigma = np.clip(1.0 - 6.8756e-6 * np.asarray(alt_ft, dtype=float), 0.2, 1.2) ** 4.2559
    return cas_kt / np.sqrt(sigma)


class Winds:
    """GFS winds by hour, from the cache only (never downloads)."""

    def __init__(self, icao: str = "egkk"):
        self.icao = icao.lower()
        self._hours: dict[str, WindProfile | None] = {}

    def at(self, t: float) -> WindProfile | None:
        key = datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y%m%d%H")
        if key not in self._hours:
            path = CACHE_DIR / f"{self.icao}_{key}.json"
            self._hours[key] = WindProfile.load(path) if path.is_file() else None
        return self._hours[key]


def _segments(track: list[list], winds: Winds, airport) -> list[dict[str, np.ndarray]]:
    """The track on an even 5 s grid, split at gaps, with the air vector worked out from the wind."""
    pts = [p for p in track if p[1] is not None and p[3] not in (None, "ground") and p[4] is not None
           and p[5] is not None]
    out, cur = [], []
    for p in pts:
        if cur and p[0] - cur[-1][0] > MAX_GAP_S:
            out.append(cur)
            cur = []
        cur.append(p)
    out.append(cur)
    segs = []
    for c in out:
        if len(c) < 6:
            continue
        a = np.array([[p[0], p[1], p[2], float(p[3]), p[4], p[5], p[6] if p[6] is not None else np.nan] for p in c])
        t0, t1 = a[0, 0], a[-1, 0]
        t = np.arange(t0, t1 + 1e-6, STEP_S)
        trk = np.degrees(np.unwrap(np.radians(a[:, 5])))
        lat, lon = np.interp(t, a[:, 0], a[:, 1]), np.interp(t, a[:, 0], a[:, 2])
        alt, gs = np.interp(t, a[:, 0], a[:, 3]), np.interp(t, a[:, 0], a[:, 4])
        trk_t = np.interp(t, a[:, 0], trk)
        ok = ~np.isnan(a[:, 6])
        vr = np.interp(t, a[ok, 0], a[ok, 6]) if ok.sum() >= 2 else np.full_like(t, np.nan)
        wind = winds.at(t0)
        if wind is None:
            continue
        u = np.empty_like(t)
        v = np.empty_like(t)
        for i in range(0, len(t), 3):  # the wind changes slowly: every 15 s is plenty
            u[i:i + 3], v[i:i + 3] = wind.uv(lat[i], lon[i], alt[i])
        ve = gs * np.sin(np.radians(trk_t)) - u
        vn = gs * np.cos(np.radians(trk_t)) - v
        tas = np.hypot(ve, vn)
        hdg = np.degrees(np.unwrap(np.arctan2(ve, vn)))
        xy = np.array([airport.grid.to_xy(la, lo) for la, lo in zip(lat, lon)])
        segs.append({"t": t, "x": xy[:, 0], "y": xy[:, 1], "alt": alt, "gs": gs, "trk": trk_t, "vr": vr,
                     "tas": tas, "hdg": hdg, "cas": cas_from_tas(tas, alt)})
    return segs


def _runs(mask: np.ndarray) -> list[tuple[int, int]]:
    """(start, end) index pairs, end exclusive, of each run of True."""
    runs, start = [], None
    for i, m in enumerate(mask):
        if m and start is None:
            start = i
        elif not m and start is not None:
            runs.append((start, i))
            start = None
    if start is not None:
        runs.append((start, len(mask)))
    return runs


def turns(s: dict[str, np.ndarray], holding: tuple[float, float] | None) -> list[dict]:
    rate = np.gradient(s["hdg"], STEP_S)
    out = []
    for sign in (1, -1):
        for a, b in _runs(sign * rate >= TURN_MIN_RATE):
            if b - a < 3:
                continue
            total = abs(s["hdg"][min(b, len(rate) - 1)] - s["hdg"][max(a - 1, 0)])
            if total < TURN_MIN_DEG:
                continue
            r = np.abs(rate[a:b])
            plateau = r[r >= 0.7 * r.max()]
            sustained = float(np.median(plateau))
            tas = float(np.mean(s["tas"][a:b]))
            bank = math.degrees(math.atan(sustained * tas / G_KT_DEG))
            mid_t = float(s["t"][(a + b) // 2])
            out.append({"rate": sustained, "bank": bank, "tas": tas, "alt": float(np.mean(s["alt"][a:b])),
                        "turn_deg": float(total), "hold": bool(holding and holding[0] <= mid_t <= holding[1])})
    return out


def descents(s: dict[str, np.ndarray]) -> tuple[list[float], list[float]]:
    """Each descent's typical rate (ft/min, positive), and for those that end in level flight, the height
    above the new level at which the rate had fallen to half."""
    vr = s["vr"]
    if np.isnan(vr).all():
        return [], []
    vr = np.convolve(np.nan_to_num(vr), np.ones(3) / 3, mode="same")
    rates, captures = [], []
    for a, b in _runs(vr <= -DESCENT_MIN_FPM):
        if b - a < 6:
            continue
        band = (s["alt"][a:b] >= DESCENT_BAND_FT[0]) & (s["alt"][a:b] <= DESCENT_BAND_FT[1])
        if band.sum() >= 4:
            rates.append(float(-np.median(vr[a:b][band])))
        after = vr[b:b + 4]
        if len(after) == 4 and np.all(np.abs(after) < 200) and s["alt"][b] >= DESCENT_BAND_FT[0]:
            level = float(np.mean(s["alt"][b:b + 4]))
            typical = -np.median(vr[a:b])
            k = next((i for i in range(a, b) if -vr[i] < 0.5 * typical and i > (a + b) // 2), b - 1)
            captures.append(float(s["alt"][k] - level))
    return rates, captures


def on_final(s: dict[str, np.ndarray], airport, runway: str) -> np.ndarray:
    """True where the aircraft is established on final: within 15 NM, near the centreline, heading in."""
    if runway not in airport.runways:
        return np.zeros(len(s["t"]), dtype=bool)
    frame = np.array([airport.runway_frame(runway, x, y) for x, y in zip(s["x"], s["y"])])
    course = airport.runways[runway].course_true
    heading_in = np.abs((s["trk"] - course + 180.0) % 360.0 - 180.0) < 20.0
    return (frame[:, 0] > 0.5) & (frame[:, 0] < 15.0) & (np.abs(frame[:, 1]) < 0.4) & heading_in


def speed_changes(s: dict[str, np.ndarray], final: np.ndarray | None = None) -> list[dict]:
    cas = np.convolve(s["cas"], np.ones(3) / 3, mode="same")
    rate = np.gradient(cas, STEP_S)
    vr = np.nan_to_num(s["vr"])
    out = []
    for sign in (-1, 1):
        for a, b in _runs(sign * rate[1:-1] >= SPEED_MIN_RATE):
            a, b = a + 1, b + 1
            if b - a < 3:
                continue
            change = float(cas[min(b, len(cas) - 2)] - cas[max(a - 1, 1)])
            is_final = bool(final is not None and final[a:b].mean() > 0.5)
            if abs(change) < SPEED_MIN_CHANGE or (s["alt"][a] < 2000 and not is_final):
                continue
            out.append({"rate": float(np.median(rate[a:b])), "change": change, "from": float(cas[max(a - 1, 1)]),
                        "level": bool(np.median(np.abs(vr[a:b])) < 300), "alt": float(np.mean(s["alt"][a:b])),
                        "final": is_final})
    return out


def _pct(xs, qs=(0.1, 0.25, 0.5, 0.75, 0.9)) -> dict[str, float]:
    xs = np.asarray(xs, dtype=float)
    return {f"p{int(q * 100)}": round(float(np.quantile(xs, q)), 3) for q in qs} if len(xs) else {}


def measure(days: list[str], airport) -> dict:
    winds = Winds(airport.icao)
    all_turns, all_speed = [], []
    descent_by_type: dict[str, list[float]] = defaultdict(list)
    flight_descent, captures = [], []
    flights = 0
    for d in days:
        for arr in load_day(date.fromisoformat(d))["arrivals"]:
            flights += 1
            hold = tuple(arr["hold_t"]) if arr.get("hold_t") else None
            mine = []
            for s in _segments(arr["track"], winds, airport):
                all_turns += turns(s, hold)
                r, c = descents(s)
                mine += r
                captures += c
                all_speed += speed_changes(s, on_final(s, airport, arr["runway"]))
            if mine:
                flight_descent.append(float(np.median(mine)))
                descent_by_type[arr["type"]].append(float(np.median(mine)))

    vectored = [t for t in all_turns if not t["hold"]]
    by_size = {}
    for lo, hi in [(10, 15), (15, 25), (25, 45), (45, 90), (90, 360)]:
        sel = [t["bank"] for t in vectored if lo <= t["turn_deg"] < hi]
        by_size[f"{lo}-{hi}"] = {"turns": len(sel), **_pct(sel)}
    by_tas = {}
    for lo in range(140, 300, 20):
        sel = [t for t in vectored if lo <= t["tas"] < lo + 20 and t["turn_deg"] >= 20]
        if len(sel) >= 30:
            by_tas[f"{lo}-{lo + 20}"] = {"turns": len(sel), "rate": _pct([t["rate"] for t in sel]),
                                         "bank": _pct([t["bank"] for t in sel])}
    decel = [s for s in all_speed if s["rate"] < 0]
    accel = [s for s in all_speed if s["rate"] > 0]
    return {
        "source": "Training days of real Gatwick arrivals (adsb.lol, ODbL 1.0), winds from NOAA GFS",
        "days": len(days), "flights": flights,
        "turns": {
            "count": len(vectored), "held_count": len(all_turns) - len(vectored),
            "note": "Turns of 10 degrees or more outside holds; bank is the angle that gives the sustained rate of turn "
                    "at the true airspeed. Small turns are flown with less bank (by_turn_size_deg), as autopilots bank "
                    "in proportion to the heading still to turn up to a limit: bank_deg, from turns of 45 degrees or "
                    "more, is that limit. by_true_airspeed uses turns of 20 degrees or more.",
            "rate_deg_s": _pct([t["rate"] for t in vectored]),
            "bank_deg": _pct([t["bank"] for t in vectored if t["turn_deg"] >= 45]),
            "by_turn_size_deg": by_size,
            "by_true_airspeed_kt": by_tas,
            "in_holds_rate_deg_s": _pct([t["rate"] for t in all_turns if t["hold"]]),
        },
        "descent": {
            "note": "Typical rate per flight while descending between 3,500 and 11,000 ft (ft/min), and the height "
                    "above the new level at which the rate has halved (ft)",
            "flights": len(flight_descent),
            "rate_fpm": _pct(flight_descent),
            "by_type": {k: {"flights": len(v), **_pct(v)} for k, v in sorted(descent_by_type.items())
                        if len(v) >= 40},
            "level_off_start_ft": _pct(captures),
        },
        "speed": {
            "note": "Rates of change of indicated airspeed (kt/s) in speed changes of 8 kt or more: above 2,000 ft, "
                    "and separately on final (established within 15 NM), where flaps and gear slow aircraft faster",
            "decelerations": len(decel), "accelerations": len(accel),
            "decel_level_kt_s": _pct([-s["rate"] for s in decel if s["level"] and not s["final"]]),
            "decel_descending_kt_s": _pct([-s["rate"] for s in decel if not s["level"] and not s["final"]]),
            "decel_final_kt_s": _pct([-s["rate"] for s in decel if s["final"]]),
            "final_decelerations": sum(1 for s in decel if s["final"]),
            "accel_kt_s": _pct([s["rate"] for s in accel]),
        },
    }


def split_days(split: str, airport_icao: str = "egkk") -> list[str]:
    rows = csv.DictReader((REAL_DIR / airport_icao / "days.csv").open())
    return [r["day"] for r in rows if r["split"] == split]


def main() -> None:
    ap = argparse.ArgumentParser(description="Measure how real arrivals turn, descend and change speed")
    ap.add_argument("--split", default="train")
    ap.add_argument("--out", default=str(REAL_DIR / "egkk" / "response.json"))
    args = ap.parse_args()
    from approach.airports import egkk

    result = measure(split_days(args.split), egkk.load())
    Path(args.out).write_text(json.dumps(result, indent=1) + "\n")
    print(json.dumps(result, indent=1))


if __name__ == "__main__":
    main()
