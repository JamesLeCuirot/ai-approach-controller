"""Real winds from NOAA's GFS weather model, for one hour around one airport.

GFS runs four times a day (00, 06, 12 and 18 UTC) and its output is free on
NOAA's open data store on AWS, from 2021 onwards, on a 0.25 degree grid (about
every 17 by 28 km at Gatwick). For a given hour we take the run that started
at or before it and its forecast for that hour (0-5 hours ahead), so every
hour of every day has its own winds.

Only the fields we need are downloaded, by byte range: the wind (u, v) and the
height of each pressure level from 1000 hPa to 600 hPa (the surface to about
14,000 ft), plus the 10 m wind. We cut a box around the airport and cache it as
a small JSON file in data/raw/gfs/, so each hour is downloaded once.

NOAA data is in the public domain. We still credit it.

    python -m approach.data.gfs 2025-01-15T12
"""

from __future__ import annotations

import argparse
import json
import math
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

BUCKET = "https://noaa-gfs-bdp-pds.s3.amazonaws.com"
LEVELS_HPA = (1000, 975, 950, 925, 900, 850, 800, 750, 700, 650, 600)
GRID_DEG = 0.25
CACHE_DIR = Path(__file__).resolve().parents[2] / "data" / "raw" / "gfs"
KT_PER_MS = 1.943844
FT_PER_M = 3.280840


def parse_time(text: str) -> datetime:
    """'2025-01-15T12', '2025-01-15T12:00' or '2025-01-15 12:00Z' -> that hour in UTC."""
    t = text.strip().rstrip("Z").replace(" ", "T")
    if len(t) == 13:
        t += ":00"
    return datetime.fromisoformat(t).replace(minute=0, second=0, microsecond=0, tzinfo=timezone.utc)


def _cycle(valid: datetime) -> tuple[datetime, int]:
    run = valid.replace(hour=valid.hour - valid.hour % 6)
    return run, int((valid - run).total_seconds() // 3600)


def _url(run: datetime, fhr: int) -> str:
    d, h = run.strftime("%Y%m%d"), run.strftime("%H")
    return f"{BUCKET}/gfs.{d}/{h}/atmos/gfs.t{h}z.pgrb2.0p25.f{fhr:03d}"


def _get(url: str, byte_range: tuple[int, int] | None = None) -> bytes:
    headers = {"User-Agent": "ai-approach-controller"}
    if byte_range:
        headers["Range"] = f"bytes={byte_range[0]}-{byte_range[1]}"
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=120) as r:
        return r.read()


@dataclass
class WindProfile:
    """Wind on a small grid around the airport, at several heights.

    u is towards the east and v towards the north, in knots, indexed
    [level][row][column] with rows south to north and columns west to east.
    """

    valid: str  # ISO time, UTC
    source: str
    lats: list[float]
    lons: list[float]
    heights_ft: list[float]  # one per level, lowest first (the first is the 10 m wind)
    u_kt: list[list[list[float]]]
    v_kt: list[list[list[float]]]

    @classmethod
    def load(cls, path: Path) -> "WindProfile":
        return cls(**json.loads(Path(path).read_text()))

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(vars(self)))

    def _bilinear(self, field: list[list[float]], lat: float, lon: float) -> float:
        fi = (min(max(lat, self.lats[0]), self.lats[-1]) - self.lats[0]) / GRID_DEG
        fj = (min(max(lon, self.lons[0]), self.lons[-1]) - self.lons[0]) / GRID_DEG
        i0, j0 = min(int(fi), len(self.lats) - 2), min(int(fj), len(self.lons) - 2)
        a, b = fi - i0, fj - j0
        return ((1 - a) * (1 - b) * field[i0][j0] + (1 - a) * b * field[i0][j0 + 1]
                + a * (1 - b) * field[i0 + 1][j0] + a * b * field[i0 + 1][j0 + 1])

    def uv(self, lat: float, lon: float, alt_ft: float) -> tuple[float, float]:
        """Wind components (towards east, towards north) in knots at a point."""
        h = self.heights_ft
        if alt_ft <= h[0]:
            k, w = 0, 0.0
        elif alt_ft >= h[-1]:
            k, w = len(h) - 2, 1.0
        else:
            k = max(i for i in range(len(h) - 1) if h[i] <= alt_ft)
            w = (alt_ft - h[k]) / (h[k + 1] - h[k])
        u = (1 - w) * self._bilinear(self.u_kt[k], lat, lon) + w * self._bilinear(self.u_kt[k + 1], lat, lon)
        v = (1 - w) * self._bilinear(self.v_kt[k], lat, lon) + w * self._bilinear(self.v_kt[k + 1], lat, lon)
        return u, v

    def wind(self, lat: float, lon: float, alt_ft: float) -> tuple[float, float]:
        """(direction the wind blows from in degrees true, speed in knots)."""
        u, v = self.uv(lat, lon, alt_ft)
        return (math.degrees(math.atan2(-u, -v)) % 360.0, math.hypot(u, v))


def fetch(valid: datetime, lat: float, lon: float, half_lat: float = 1.5, half_lon: float = 2.0,
          cache_dir: Path = CACHE_DIR, airport: str = "") -> WindProfile:
    """The GFS winds for one hour in a box around (lat, lon), from the cache or NOAA."""
    valid = valid.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
    tag = f"{airport.lower()}_" if airport else f"{lat:.2f}_{lon:.2f}_"
    path = cache_dir / f"{tag}{valid.strftime('%Y%m%d%H')}.json"
    if path.is_file():
        return WindProfile.load(path)

    import pygrib  # only needed when downloading

    run, fhr = _cycle(valid)
    url = _url(run, fhr)
    try:
        lines = _get(url + ".idx").decode().splitlines()
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"No GFS data for {valid:%Y-%m-%d %H}Z at {url} ({e}). NOAA's AWS store starts in 2021.") from e
    rows = [ln.split(":") for ln in lines]
    offsets = [int(r[1]) for r in rows]
    wanted = {("UGRD", "10 m above ground"), ("VGRD", "10 m above ground"), ("HGT", "surface")}
    for p in LEVELS_HPA:
        wanted |= {("UGRD", f"{p} mb"), ("VGRD", f"{p} mb"), ("HGT", f"{p} mb")}

    # Grid rows run north to south from 90N, columns east from 0E.
    n_rows, n_cols = int(180 / GRID_DEG) + 1, int(360 / GRID_DEG)
    lat0 = math.floor((lat - half_lat) / GRID_DEG) * GRID_DEG
    lat1 = math.ceil((lat + half_lat) / GRID_DEG) * GRID_DEG
    lon0 = math.floor((lon - half_lon) / GRID_DEG) * GRID_DEG
    lon1 = math.ceil((lon + half_lon) / GRID_DEG) * GRID_DEG
    lats = [round(lat0 + i * GRID_DEG, 2) for i in range(round((lat1 - lat0) / GRID_DEG) + 1)]
    lons = [round(lon0 + j * GRID_DEG, 2) for j in range(round((lon1 - lon0) / GRID_DEG) + 1)]
    ri = [round((90.0 - la) / GRID_DEG) for la in lats]
    cj = [round((lo % 360.0) / GRID_DEG) % n_cols for lo in lons]

    fields: dict[tuple[str, str], list[list[float]]] = {}
    for k, r in enumerate(rows):
        key = (r[3], r[4])
        if key not in wanted:
            continue
        end = offsets[k + 1] - 1 if k + 1 < len(rows) else None
        grb = pygrib.fromstring(_get(url, (offsets[k], end)) if end else _get(url))
        values = grb.values
        assert values.shape == (n_rows, n_cols), values.shape
        fields[key] = [[float(values[i, j]) for j in cj] for i in ri]

    missing = wanted - fields.keys()
    if missing:
        raise RuntimeError(f"GFS file {url} is missing {sorted(missing)}")

    def mean_ft(grid: list[list[float]]) -> float:
        return sum(map(sum, grid)) / (len(lats) * len(lons)) * FT_PER_M

    # Heights are above sea level, like aircraft altitudes. The 10 m wind goes 10 m above the average
    # ground height in the box; pressure levels at or below it (1000 hPa on a low-pressure day) are dropped.
    heights = [mean_ft(fields[("HGT", "surface")]) + 10.0 * FT_PER_M]
    us, vs = [fields[("UGRD", "10 m above ground")]], [fields[("VGRD", "10 m above ground")]]
    for p in LEVELS_HPA:
        h_ft = mean_ft(fields[("HGT", f"{p} mb")])
        if h_ft > heights[-1] + 300:
            heights.append(h_ft)
            us.append(fields[("UGRD", f"{p} mb")])
            vs.append(fields[("VGRD", f"{p} mb")])

    def kt(grid: list[list[float]]) -> list[list[float]]:
        return [[round(x * KT_PER_MS, 1) for x in row] for row in grid]

    prof = WindProfile(
        valid=valid.strftime("%Y-%m-%dT%H:00Z"),
        source=f"NOAA GFS 0.25 degree, run {run:%Y-%m-%d %H}Z + {fhr} h",
        lats=lats, lons=lons, heights_ft=[round(h) for h in heights],
        u_kt=[kt(g) for g in us], v_kt=[kt(g) for g in vs],
    )
    prof.save(path)
    return prof


def fetch_for_airport(valid: datetime, airport) -> WindProfile:
    return fetch(valid, airport.reference.lat, airport.reference.lon, airport=airport.icao)


def _prefetch_one(hour: str) -> str:
    from approach.airports import egkk

    try:
        fetch_for_airport(parse_time(hour), egkk.load())
        return ""
    except Exception as e:  # noqa: BLE001 - report and carry on with the other hours
        return f"{hour}: {e}"


def prefetch(splits: set[str], workers: int = 3) -> None:
    """Download the winds for every hour of real traffic on the chosen days (training needs them all)."""
    import csv
    from multiprocessing import Pool

    from approach.data.arrivals import REAL_DIR

    rows = csv.DictReader((REAL_DIR / "egkk" / "real_controller_hours.csv").open())
    hours = sorted({f"{r['day']}T{int(r['hour']):02d}" for r in rows if r["split"] in splits})
    todo = [h for h in hours if not (CACHE_DIR / f"egkk_{h.replace('-', '').replace('T', '')}.json").is_file()]
    print(f"{len(hours)} hours, {len(todo)} to download", flush=True)
    with Pool(workers) as pool:
        for k, err in enumerate(pool.imap_unordered(_prefetch_one, todo), 1):
            if err:
                print(err, flush=True)
            if k % 50 == 0:
                print(f"{k}/{len(todo)}", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser(description="Download GFS winds for one hour at Gatwick and print the profile, "
                                             "or with --prefetch for every hour of real traffic on the chosen days")
    ap.add_argument("time", nargs="?", help="UTC hour, e.g. 2025-01-15T12")
    ap.add_argument("--prefetch", nargs="+", metavar="SPLIT", help="splits to download winds for, e.g. train test")
    args = ap.parse_args()
    if args.prefetch:
        prefetch(set(args.prefetch))
        return
    from approach.airports import egkk

    airport = egkk.load()
    prof = fetch_for_airport(parse_time(args.time), airport)
    lat, lon = airport.reference.lat, airport.reference.lon
    print(f"{prof.valid}  {prof.source}")
    for h in [prof.heights_ft[0], 1000, 2000, 3000, 4000, 6000, 8000, 10000]:
        d, s = prof.wind(lat, lon, h)
        print(f"  {h:>6.0f} ft  {d:03.0f}/{s:.0f} kt")


if __name__ == "__main__":
    main()
