"""adsb.lol's live feed around an airport, recorded in the same form as a day of its open data.

adsb.lol (https://www.adsb.lol) serves every aircraft its receivers can see now, free and without a key, at
api.adsb.lol/v2/point/{lat}/{lon}/{radius NM}. Its data is under the Open Database Licence (ODbL 1.0), as the
archived days are. A Recorder polls it every few seconds and keeps every aircraft inside the same area as
approach.data.adsblol (50 NM, below 20,000 ft), with the same point fields, so approach.data.arrivals can find
the real arrivals in a recording exactly as it does in an archived day.
"""

from __future__ import annotations

import gzip
import json
import math
import time
import urllib.request
from pathlib import Path

from approach.data.adsblol import CEILING_FT, CREDIT, RADIUS_NM

URL = "https://api.adsb.lol/v2/point/{lat:.4f}/{lon:.4f}/{radius:.0f}"
LIVE_DIR = Path(__file__).resolve().parents[2] / "data" / "raw" / "live"
MAX_AGE_S = 15.0  # a position older than this when polled is skipped
FIELDS = ["t", "lat", "lon", "alt", "gs", "track", "vrate", "callsign", "flags", "ias"]


def poll(lat: float, lon: float, radius_nm: float = RADIUS_NM + 5, timeout: float = 15.0) -> tuple[float, list[dict]]:
    """The feed now: (time it was served, seconds since 1970; the aircraft as adsb.lol gives them)."""
    req = urllib.request.Request(URL.format(lat=lat, lon=lon, radius=radius_nm),
                                 headers={"User-Agent": "ai-approach-controller"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        d = json.loads(r.read())
    return float(d.get("now", time.time() * 1000)) / 1000.0, d.get("ac", [])


def to_point(a: dict, now: float) -> list | None:
    """One aircraft from the feed as a point of an archived day: t, lat, lon, alt, gs, track, vrate, callsign,
    flags, ias. None if it has no recent position."""
    if a.get("lat") is None or a.get("lon") is None or float(a.get("seen_pos", 0.0)) > MAX_AGE_S:
        return None
    alt = a.get("alt_baro")
    if alt not in (None, "ground"):
        alt = int(alt)
    rate = a.get("baro_rate", a.get("geom_rate"))
    return [round(now - float(a.get("seen_pos", 0.0)), 1), round(float(a["lat"]), 5), round(float(a["lon"]), 5), alt,
            a.get("gs"), a.get("track"), rate, (a.get("flight") or "").strip(), 0, a.get("ias")]


class Recorder:
    """Every aircraft seen inside the area since it started, with its points, as approach.data.adsblol keeps a day."""

    def __init__(self, lat0: float, lon0: float, radius_nm: float = RADIUS_NM, ceiling_ft: float = CEILING_FT):
        self.lat0, self.lon0 = lat0, lon0
        self.radius_nm, self.ceiling_ft = radius_nm, ceiling_ft
        self.coslat = math.cos(math.radians(lat0))
        self.aircraft: dict[str, dict] = {}
        self.started = time.time()
        self.polls = 0
        self.failed = 0

    def inside(self, p: list) -> bool:
        if p[3] not in (None, "ground") and p[3] > self.ceiling_ft:
            return False
        return math.hypot((p[1] - self.lat0) * 60.0, (p[2] - self.lon0) * 60.0 * self.coslat) <= self.radius_nm

    def add(self, now: float, feed: list[dict]) -> list[tuple[str, list]]:
        """Add one poll's aircraft; returns the new points inside the area, as (hex, point)."""
        self.polls += 1
        new = []
        for a in feed:
            hexid = a.get("hex")
            p = to_point(a, now) if hexid else None
            if p is None or not self.inside(p):
                continue
            ac = self.aircraft.setdefault(hexid, {"hex": hexid, "type": a.get("t"), "reg": a.get("r"), "points": []})
            if ac["points"] and p[0] <= ac["points"][-1][0]:
                continue  # no new position since the last poll
            ac["points"].append(p)
            new.append((hexid, p))
        return new

    def area(self) -> dict:
        """The recording so far, in the form of an archived day (approach.data.adsblol.filter_day)."""
        return {
            "day": time.strftime("%Y-%m-%d", time.gmtime(self.started)), "source": CREDIT, "instance": "live",
            "aircraft_read": len(self.aircraft), "centre": [self.lat0, self.lon0], "radius_nm": self.radius_nm,
            "ceiling_ft": self.ceiling_ft, "fields": FIELDS,
            "aircraft": [ac for ac in self.aircraft.values() if ac["points"]],
        }

    def save(self, path: Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        with gzip.open(tmp, "wt") as f:
            json.dump(self.area(), f, separators=(",", ":"))
        tmp.replace(path)
        return path


def record(path: Path, minutes: float, every_s: float = 5.0, airport=None, log=print) -> Path:
    """Poll for `minutes`, appending each poll's new points to `path` as a line of JSON: {"now": t, "new": [[hex,
    type, registration, point], ...]}. A reader (approach.live.shadow) follows the file as it grows; read_area
    turns it back into an archived day's form."""
    from approach.airports import egkk

    airport = airport or egkk.load()
    lat0, lon0 = airport.reference.lat, airport.reference.lon
    rec = Recorder(lat0, lon0)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    end = time.time() + minutes * 60
    types: dict[str, tuple] = {}
    with open(path, "a", encoding="utf-8") as f:
        while time.time() < end:
            t_poll = time.time()
            try:
                now, feed = poll(lat0, lon0)
            except Exception as e:  # noqa: BLE001  (a dropped poll is skipped, as a receiver gap would be)
                rec.failed += 1
                if log and rec.failed % 12 == 1:
                    log(f"poll failed ({rec.failed} so far): {e!r}")
            else:
                for a in feed:
                    if a.get("hex"):
                        types[a["hex"]] = (a.get("t"), a.get("r"))
                new = rec.add(now, feed)
                f.write(json.dumps({"now": now, "new": [[h, *types.get(h, (None, None)), p] for h, p in new]},
                                   separators=(",", ":")) + "\n")
                f.flush()
            time.sleep(max(0.0, every_s - (time.time() - t_poll)))
    return path


def read_area(path: Path, lat0: float, lon0: float, start: float | None = None) -> dict:
    """A recording (record's file) as an archived day (approach.data.adsblol.filter_day)."""
    aircraft: dict[str, dict] = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            try:
                d = json.loads(line)
            except ValueError:  # a line still being written
                continue
            for hexid, actype, reg, p in d["new"]:
                ac = aircraft.setdefault(hexid, {"hex": hexid, "type": actype, "reg": reg, "points": []})
                ac["points"].append(p)
    first = start or min((ac["points"][0][0] for ac in aircraft.values()), default=time.time())
    return {"day": time.strftime("%Y-%m-%d", time.gmtime(first)), "source": CREDIT, "instance": "live",
            "aircraft_read": len(aircraft), "centre": [lat0, lon0], "radius_nm": RADIUS_NM, "ceiling_ft": CEILING_FT,
            "fields": FIELDS, "aircraft": list(aircraft.values())}


def main() -> None:
    import argparse

    ap = argparse.ArgumentParser(description="Record adsb.lol's live feed around Gatwick")
    ap.add_argument("--out", required=True)
    ap.add_argument("--minutes", type=float, default=60.0)
    ap.add_argument("--every", type=float, default=5.0, help="seconds between polls")
    args = ap.parse_args()
    record(Path(args.out), args.minutes, args.every)


if __name__ == "__main__":
    main()
