"""London Gatwick (EGKK).

Geometry (fixes, runway thresholds, vectoring areas, minimum-altitude areas) comes
from VATSIM UK's sector file, fetched by ``scripts/fetch_sector_file.sh``. Procedure
values (holds, levels, elevations, variation) come from docs/stage0-gatwick-spec.md.
"""

from __future__ import annotations

from pathlib import Path

from approach.airports.model import Airport, Hold, Runway
from approach.airports.sectorfile import (
    DEFAULT_SECTOR_DIR,
    read_points,
    read_runways,
    read_segments,
    require,
)
from approach.geo import LatLon, great_circle_bearing, great_circle_distance_nm

ICAO = "EGKK"

# From the stage 0 spec (UK AIP AD 2.12 / 2.13 and the Gatwick vMATS).
RUNWAY_DATA = {
    "26L": {"elevation_ft": 195.9, "landing_distance_m": 2830},
    "08R": {"elevation_ft": 196.2, "landing_distance_m": 2765},
}
HOLDS = [
    # fix, inbound course (magnetic), turn, lowest (None = MSL), highest (ft)
    ("WILLO", 284, "left", None, 15000),
    ("TIMBA", 309, "right", None, 15000),
    ("MAY", 87, "left", 3000, 6000),
]
MAGNETIC_VARIATION_EAST = 1.33
TRANSITION_ALTITUDE_FT = 6000
MIN_ASSIGNABLE_ALTITUDE_FT = 2000
FIX_RADIUS_NM = 80.0
# Fixes drawn on the radar replay: holds, the ends of the arrival routes, nearby beacons.
DISPLAY_FIXES = [
    "WILLO", "TIMBA", "HOLLY", "LARCK", "KKE63", "KKE64", "MAY", "SFD",
    "OCK", "MID", "BIG", "DET", "GWC", "TELTU", "HASTY", "OLEVI", "ABIBI",
]


def load(sector_dir: Path | None = None) -> Airport:
    sector_dir = require(Path(sector_dir) if sector_dir else DEFAULT_SECTOR_DIR)
    apt_dir = sector_dir / "Airports" / ICAO

    basic = [l.strip() for l in (apt_dir / "Basic.txt").read_text().splitlines() if l.strip()]
    name = basic[0]
    lat_txt, lon_txt = basic[1].split()[:2]
    from approach.geo import parse_sector_latlon

    reference = parse_sector_latlon(lat_txt, lon_txt)

    fixes: dict[str, LatLon] = {}
    navaids = sector_dir / "Navaids"
    for fname in ("FIXES_UK.txt", "VOR_UK.txt"):
        fixes.update(read_points(navaids / fname))
    if (apt_dir / "Fixes.txt").exists():
        fixes.update(read_points(apt_dir / "Fixes.txt"))
    fixes = {n: p for n, p in fixes.items() if great_circle_distance_nm(reference, p) <= FIX_RADIUS_NM}

    thresholds = read_runways(apt_dir / "Runway.txt")
    runways = {}
    for rwy, extra in RUNWAY_DATA.items():
        own, opposite = thresholds[rwy]
        runways[rwy] = Runway(
            name=rwy,
            threshold=own,
            course_true=great_circle_bearing(own, opposite),
            elevation_ft=extra["elevation_ft"],
            landing_distance_m=extra["landing_distance_m"],
        )

    holds = {
        fix: Hold(fix=fix, position=fixes[fix], inbound_course_mag=course, turn=turn, lowest_ft=low, highest_ft=high)
        for fix, course, turn, low, high in HOLDS
    }

    airspace = {}
    for fname in ("RMA.txt", "SMAA.txt"):
        if (apt_dir / fname).exists():
            airspace.update(read_segments(apt_dir / fname))

    return Airport(
        icao=ICAO,
        name=name,
        reference=reference,
        magnetic_variation_east=MAGNETIC_VARIATION_EAST,
        transition_altitude_ft=TRANSITION_ALTITUDE_FT,
        min_assignable_altitude_ft=MIN_ASSIGNABLE_ALTITUDE_FT,
        runways=runways,
        holds=holds,
        fixes=fixes,
        airspace=airspace,
        # Westerlies (26L) and easterlies (08R) have different RMAs, as drawn in the sector file's RMA.txt.
        rma_shapes={"26L": "Gatwick RMA Westerlies", "26R": "Gatwick RMA Westerlies",
                    "08R": "Gatwick RMA Easterlies", "08L": "Gatwick RMA Easterlies"},
        smaa_shape="EGKK Gatwick SMAA",
    )
