"""Real traffic from adsb.lol's open data, one day at a time, around one airport.

adsb.lol (https://www.adsb.lol) publishes every aircraft its receivers saw each day,
for the whole world, as a GitHub release: one tar file, or parts of up to 2 GB,
about 3.5 GB a day. Its data runs from late December 2023 and is under the Open
Database Licence (ODbL 1.0): free to use, but anything built from it must credit
adsb.lol and stay under the same licence.

We stream a day's parts straight through a filter, never storing them, and keep
every aircraft that came within RADIUS_NM of the airport below CEILING_FT, with
the points inside that area. For Gatwick that is about 70 MB a day, kept in
data/raw/adsblol/ (not committed). approach.data.arrivals then cuts it down to
the arrivals.

    python -m approach.data.adsblol 2025-06-27 2024-12-07
"""

from __future__ import annotations

import argparse
import gzip
import io
import json
import math
import tarfile
import time
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path

RADIUS_NM = 50.0
CEILING_FT = 20000
INSTANCES = ("prod-0", "staging-0")  # prod powers adsb.lol; staging is the fallback for a missing day
RAW_DIR = Path(__file__).resolve().parents[2] / "data" / "raw" / "adsblol"
CREDIT = "adsb.lol (https://www.adsb.lol), ODbL 1.0"


def _tag(day: date, instance: str) -> str:
    return f"v{day:%Y.%m.%d}-planes-readsb-{instance}"


def _url(day: date, instance: str, name: str) -> str:
    tag = _tag(day, instance)
    return f"https://github.com/adsblol/globe_history_{day.year}/releases/download/{tag}/{name}"


def _exists(url: str) -> bool:
    req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "ai-approach-controller"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status == 200
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return False
        raise


def part_urls(day: date) -> tuple[str, list[str]]:
    """The instance used and the URLs of a day's file or parts, in order."""
    for instance in INSTANCES:
        tag = _tag(day, instance)
        whole = _url(day, instance, f"{tag}.tar")
        if _exists(whole):
            return instance, [whole]
        parts = []
        for k in range(26):
            u = _url(day, instance, f"{tag}.tar.a{chr(ord('a') + k)}")
            if not _exists(u):
                break
            parts.append(u)
        if parts:
            return instance, parts
    raise FileNotFoundError(f"adsb.lol has no data for {day}")


class _Concat(io.RawIOBase):
    """Read several HTTP bodies one after another as one stream."""

    def __init__(self, urls: list[str]):
        self.urls, self.cur = list(urls), None

    def readable(self) -> bool:
        return True

    def readinto(self, b) -> int:
        while True:
            if self.cur is None:
                if not self.urls:
                    return 0
                req = urllib.request.Request(self.urls.pop(0), headers={"User-Agent": "ai-approach-controller"})
                self.cur = urllib.request.urlopen(req, timeout=300)
            n = self.cur.readinto(b)
            if n:
                return n
            self.cur.close()
            self.cur = None


def filter_day(day: date, lat0: float, lon0: float, radius_nm: float = RADIUS_NM, ceiling_ft: float = CEILING_FT,
               log=print) -> dict:
    """Every aircraft seen within radius_nm of (lat0, lon0) below ceiling_ft on that day (UTC)."""
    instance, urls = part_urls(day)
    coslat = math.cos(math.radians(lat0))
    dlat, dlon = radius_nm / 60.0, radius_nm / 60.0 / coslat
    kept, seen, t0 = [], 0, time.time()
    stream = io.BufferedReader(_Concat(urls), buffer_size=1 << 20)
    with tarfile.open(fileobj=stream, mode="r|") as tar:
        for m in tar:
            if not m.isfile() or "trace" not in m.name:
                continue
            seen += 1
            raw = tar.extractfile(m).read()
            if raw[:2] == b"\x1f\x8b":
                raw = gzip.decompress(raw)
            try:
                d = json.loads(raw)
            except ValueError:
                continue
            base = float(d.get("timestamp", 0))
            pts = []
            for p in d.get("trace", []):
                lat, lon, alt = p[1], p[2], p[3]
                if lat is None or lon is None or abs(lat - lat0) > dlat or abs(lon - lon0) > dlon:
                    continue
                if alt is not None and alt != "ground" and alt > ceiling_ft:
                    continue
                if math.hypot((lat - lat0) * 60.0, (lon - lon0) * 60.0 * coslat) > radius_nm:
                    continue
                info = p[8] if len(p) > 8 and isinstance(p[8], dict) else None
                pts.append([
                    round(base + p[0], 1), round(lat, 5), round(lon, 5), alt,
                    p[4], p[5], p[7] if len(p) > 7 else None,  # ground speed kt, track deg, vertical rate ft/min
                    (info.get("flight") or "").strip() if info else "",
                    p[6] if len(p) > 6 else 0,  # flags: 1 stale, 2 new leg
                    p[12] if len(p) > 12 else None,  # indicated airspeed when known
                ])
            if pts:
                kept.append({"hex": d.get("icao"), "type": d.get("t"), "reg": d.get("r"), "points": pts})
            if log and seen % 20000 == 0:
                log(f"  {day}: {seen} aircraft read, {len(kept)} in the area, {time.time() - t0:.0f} s")
    return {
        "day": day.isoformat(), "source": CREDIT, "instance": instance, "aircraft_read": seen,
        "centre": [lat0, lon0], "radius_nm": radius_nm, "ceiling_ft": ceiling_ft,
        "fields": ["t", "lat", "lon", "alt", "gs", "track", "vrate", "callsign", "flags", "ias"],
        "aircraft": kept,
    }


def area_path(airport_icao: str, day: date) -> Path:
    return RAW_DIR / airport_icao.lower() / f"{day.isoformat()}.json.gz"


def get_day(day: date, airport, log=print) -> dict:
    """A day's area traffic around the airport, from the cache or adsb.lol."""
    path = area_path(airport.icao, day)
    if path.is_file():
        with gzip.open(path, "rt") as f:
            return json.load(f)
    data = filter_day(day, airport.reference.lat, airport.reference.lon, log=log)
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt") as f:
        json.dump(data, f, separators=(",", ":"))
    return data


def main() -> None:
    ap = argparse.ArgumentParser(description="Download days of adsb.lol traffic around Gatwick")
    ap.add_argument("days", nargs="+", help="UTC days, e.g. 2025-06-27")
    args = ap.parse_args()
    from approach.airports import egkk

    airport = egkk.load()
    for d in args.days:
        day = date.fromisoformat(d)
        t = time.time()
        for attempt in range(3):  # the downloads are long; a dropped connection just means starting the day again
            try:
                data = get_day(day, airport)
                break
            except (OSError, EOFError, tarfile.TarError) as e:
                print(f"{day}: attempt {attempt + 1} failed ({e}), retrying", flush=True)
                time.sleep(30)
        else:
            print(f"{day}: gave up", flush=True)
            continue
        print(f"{day}: {len(data['aircraft'])} aircraft in the area ({data['instance']}), {time.time() - t:.0f} s", flush=True)


if __name__ == "__main__":
    main()
