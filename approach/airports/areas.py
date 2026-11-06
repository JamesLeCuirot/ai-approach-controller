"""Areas from the sector file's drawings: the RMA for each runway direction and the minimum-altitude area.

The sector file draws these for display, as lines and short dashes (the RMA's 30 NM
edge is a dashed arc), with dividing lines inside. To use them as areas we join the
lines with a small tolerance, take the outer outline and fill it.
"""

from __future__ import annotations

from shapely import contains_xy
from shapely.geometry import LineString, MultiPolygon, Point, Polygon
from shapely.ops import unary_union

JOIN_TOLERANCE_NM = 0.25


def area_from_segments(segments_xy: list[tuple[tuple[float, float], tuple[float, float]]]) -> Polygon:
    lines = unary_union([LineString([a, b]) for a, b in segments_xy if a != b])
    thick = lines.buffer(JOIN_TOLERANCE_NM, join_style="mitre")
    if isinstance(thick, MultiPolygon):
        thick = max(thick.geoms, key=lambda g: g.area)
    return Polygon(thick.exterior).buffer(-JOIN_TOLERANCE_NM, join_style="mitre")


class Area:
    """A filled area in the airport's local grid (NM), with fast point tests."""

    def __init__(self, name: str, polygon: Polygon):
        self.name = name
        self.polygon = polygon

    def contains(self, x: float, y: float) -> bool:
        return bool(contains_xy(self.polygon, x, y))

    def distance_nm(self, x: float, y: float) -> float:
        """How far outside the area a point is (0 inside)."""
        return 0.0 if self.contains(x, y) else float(self.polygon.distance(Point(x, y)))
