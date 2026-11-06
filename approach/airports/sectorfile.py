"""Readers for VATSIM UK's open-source sector file (fetched by scripts/fetch_sector_file.sh)."""

from __future__ import annotations

import re
from pathlib import Path

from approach.geo import LatLon, parse_sector_latlon

DEFAULT_SECTOR_DIR = Path(__file__).resolve().parents[2] / "data" / "external" / "UK-Sector-File"

_COORD = r"[NS]\d{3}\.\d{2}\.\d{2}\.\d{3}"
_LON = r"[EW]\d{3}\.\d{2}\.\d{2}\.\d{3}"
_SEGMENT = re.compile(rf"({_COORD})\s+({_LON})\s+({_COORD})\s+({_LON})")
_POINT = re.compile(rf"({_COORD})\s+({_LON})")


class SectorFileMissing(FileNotFoundError):
    pass


def require(sector_dir: Path) -> Path:
    if not (sector_dir / "Navaids").is_dir():
        raise SectorFileMissing(
            f"VATSIM UK sector file not found at {sector_dir}. Run ./scripts/fetch_sector_file.sh first."
        )
    return sector_dir


def _strip_comment(line: str) -> str:
    return line.split(";", 1)[0].rstrip()


def read_points(path: Path) -> dict[str, LatLon]:
    """Read a fixes or navaid file: ``NAME [freq] LAT LON [; comment]``."""
    points: dict[str, LatLon] = {}
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = _strip_comment(raw)
        if not line.strip():
            continue
        name = line.split()[0]
        match = _POINT.search(line)
        if match:
            points[name] = parse_sector_latlon(match.group(1), match.group(2))
    return points


def read_segments(path: Path) -> dict[str, list[tuple[LatLon, LatLon]]]:
    """Read a named line-segment file (``Name  lat lon lat lon`` then continuation lines).

    Placeholder segments using S999/E999 coordinates are skipped.
    """
    shapes: dict[str, list[tuple[LatLon, LatLon]]] = {}
    current = None
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = _strip_comment(raw)
        if not line.strip():
            continue
        if not line[0].isspace() and not _SEGMENT.match(line):
            # A line that starts with a name (not indented, not a coordinate) opens a new shape.
            first = _SEGMENT.search(line)
            current = line[: first.start()].strip() if first else line.strip()
            shapes.setdefault(current, [])
        if current is None:
            continue
        if "999.00.00.000" in line:
            continue
        for match in _SEGMENT.finditer(line):
            a = parse_sector_latlon(match.group(1), match.group(2))
            b = parse_sector_latlon(match.group(3), match.group(4))
            shapes[current].append((a, b))
    return {name: segs for name, segs in shapes.items() if segs}


def read_runways(path: Path) -> dict[str, tuple[LatLon, LatLon]]:
    """Read ``Runway.txt``: ``08R 26L 077 257 lat lon lat lon``.

    Returns, per runway name, its own threshold and the opposite threshold.
    """
    runways: dict[str, tuple[LatLon, LatLon]] = {}
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = _strip_comment(raw)
        parts = line.split()
        if len(parts) < 8:
            continue
        a_name, b_name = parts[0], parts[1]
        a = parse_sector_latlon(parts[4], parts[5])
        b = parse_sector_latlon(parts[6], parts[7])
        runways[a_name] = (a, b)
        runways[b_name] = (b, a)
    return runways
