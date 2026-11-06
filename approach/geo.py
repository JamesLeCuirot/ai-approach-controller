"""Geography helpers.

Positions are latitude/longitude in degrees. For work inside one approach
sector (about 40 NM across) a flat local grid is accurate enough, so most of
the code works in nautical miles east (x) and north (y) of a reference point.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

NM_PER_DEG_LAT = 60.0
FT_PER_NM = 6076.12
FT_PER_M = 3.28084
KT_PER_MS = 1.943844


@dataclass(frozen=True)
class LatLon:
    lat: float
    lon: float


def parse_sector_coord(text: str) -> float:
    """Parse a sector-file coordinate such as ``N051.08.45.120`` or ``W000.12.24.520``.

    The format is hemisphere, degrees, minutes, seconds and thousandths of a second.
    """
    text = text.strip()
    hemisphere, body = text[0].upper(), text[1:]
    parts = body.split(".")
    if len(parts) != 4:
        raise ValueError(f"not a sector-file coordinate: {text!r}")
    deg, minutes, seconds, thousandths = (int(p) for p in parts)
    value = deg + minutes / 60.0 + (seconds + thousandths / 1000.0) / 3600.0
    if hemisphere in ("S", "W"):
        value = -value
    elif hemisphere not in ("N", "E"):
        raise ValueError(f"bad hemisphere in {text!r}")
    return value


def parse_sector_latlon(lat_text: str, lon_text: str) -> LatLon:
    return LatLon(parse_sector_coord(lat_text), parse_sector_coord(lon_text))


def normalise_heading(deg: float) -> float:
    """Return a heading in [0, 360)."""
    return deg % 360.0


def heading_difference(target: float, current: float) -> float:
    """Signed smallest turn from ``current`` to ``target`` in degrees (positive = right)."""
    return (target - current + 180.0) % 360.0 - 180.0


class LocalGrid:
    """Flat grid in nautical miles centred on a reference point (x east, y north)."""

    def __init__(self, ref: LatLon):
        self.ref = ref
        self._coslat = math.cos(math.radians(ref.lat))

    def to_xy(self, lat: float, lon: float) -> tuple[float, float]:
        x = (lon - self.ref.lon) * NM_PER_DEG_LAT * self._coslat
        y = (lat - self.ref.lat) * NM_PER_DEG_LAT
        return x, y

    def to_latlon(self, x: float, y: float) -> LatLon:
        lat = self.ref.lat + y / NM_PER_DEG_LAT
        lon = self.ref.lon + x / (NM_PER_DEG_LAT * self._coslat)
        return LatLon(lat, lon)


def bearing_xy(x0: float, y0: float, x1: float, y1: float) -> float:
    """True bearing in degrees from point 0 to point 1 on the local grid."""
    return normalise_heading(math.degrees(math.atan2(x1 - x0, y1 - y0)))


def distance_xy(x0: float, y0: float, x1: float, y1: float) -> float:
    return math.hypot(x1 - x0, y1 - y0)


def offset_xy(x: float, y: float, bearing_deg: float, dist_nm: float) -> tuple[float, float]:
    """Point ``dist_nm`` from (x, y) along a true bearing."""
    b = math.radians(bearing_deg)
    return x + dist_nm * math.sin(b), y + dist_nm * math.cos(b)


def great_circle_bearing(a: LatLon, b: LatLon) -> float:
    lat1, lat2 = math.radians(a.lat), math.radians(b.lat)
    dlon = math.radians(b.lon - a.lon)
    y = math.sin(dlon) * math.cos(lat2)
    x = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlon)
    return normalise_heading(math.degrees(math.atan2(y, x)))


def great_circle_distance_nm(a: LatLon, b: LatLon) -> float:
    lat1, lat2 = math.radians(a.lat), math.radians(b.lat)
    dlat, dlon = lat2 - lat1, math.radians(b.lon - a.lon)
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * math.asin(math.sqrt(h)) * 3440.065
