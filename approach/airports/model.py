"""Airports as data: runways, holds, fixes and airspace shapes."""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from approach.geo import FT_PER_NM, LatLon, LocalGrid, offset_xy

Segment = tuple[LatLon, LatLon]


@dataclass
class Runway:
    """A landing runway. ``course_true`` is the direction aircraft land in."""

    name: str
    threshold: LatLon
    course_true: float
    elevation_ft: float
    glide_slope_deg: float = 3.0
    reference_height_ft: float = 51.0  # ILS reference datum height over the threshold
    landing_distance_m: float | None = None

    def glide_path_altitude_ft(self, distance_nm: float) -> float:
        """Altitude (above sea level) of the glide path ``distance_nm`` from the threshold."""
        rise = distance_nm * FT_PER_NM * math.tan(math.radians(self.glide_slope_deg))
        return self.elevation_ft + self.reference_height_ft + rise


@dataclass
class Hold:
    fix: str
    position: LatLon
    inbound_course_mag: float
    turn: str  # "left" or "right"
    lowest_ft: int | None  # None means the minimum stack level
    highest_ft: int
    speed_kt: int = 220


@dataclass
class Airport:
    icao: str
    name: str
    reference: LatLon
    magnetic_variation_east: float
    transition_altitude_ft: int
    min_assignable_altitude_ft: int
    runways: dict[str, Runway]
    holds: dict[str, Hold]
    fixes: dict[str, LatLon]
    airspace: dict[str, list[Segment]] = field(default_factory=dict)
    minimum_stack_level_fl: int = 70

    def __post_init__(self) -> None:
        self.grid = LocalGrid(self.reference)

    def xy(self, point: LatLon) -> tuple[float, float]:
        return self.grid.to_xy(point.lat, point.lon)

    def true_to_magnetic(self, true_deg: float) -> float:
        return (true_deg - self.magnetic_variation_east) % 360.0

    def magnetic_to_true(self, mag_deg: float) -> float:
        return (mag_deg + self.magnetic_variation_east) % 360.0

    def runway_frame(self, runway: str, x: float, y: float) -> tuple[float, float]:
        """Distance from the threshold along the extended centreline (positive = out on
        the approach) and lateral offset (positive = right of an aircraft on final)."""
        rwy = self.runways[runway]
        tx, ty = self.xy(rwy.threshold)
        c = math.radians(rwy.course_true)
        ux, uy = math.sin(c), math.cos(c)  # unit vector in the landing direction
        dx, dy = x - tx, y - ty
        along = -(dx * ux + dy * uy)
        lateral = dx * uy - dy * ux  # right of the landing direction is positive
        return along, lateral

    def centreline_point(self, runway: str, along_nm: float, lateral_nm: float = 0.0) -> tuple[float, float]:
        """Local-grid point ``along_nm`` out on the approach and ``lateral_nm`` to the
        right of an aircraft on final (negative = left)."""
        rwy = self.runways[runway]
        tx, ty = self.xy(rwy.threshold)
        px, py = offset_xy(tx, ty, rwy.course_true + 180.0, along_nm)
        return offset_xy(px, py, rwy.course_true + 90.0, lateral_nm)
