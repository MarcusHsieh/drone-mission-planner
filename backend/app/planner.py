"""Grouping and path preview.

Groups: 4-connected components of the selected cells. Group numbers stay
stable across edits: a new component takes the number of the old group it
overlaps most, and brand-new components take the smallest free number.

Preview: straight lawnmower passes over one group, aligned with the group's
long axis, joined end to end. M1 has no sorties or battery math; the numbers
are estimates for the stats panel.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from shapely import affinity
from shapely.geometry import LineString, MultiLineString, Point, box
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union

from .geo import CELL_M, Cell

# Planning assumptions (placeholders until the camera and airframe are chosen).
ALTITUDE_M = 15.0
SIDELAP = 0.70
CAMERA_HFOV_DEG = 80.0
CRUISE_SPEED_MS = 5.0
TURN_PENALTY_S = 3.0


def pass_spacing_m(altitude_m: float = ALTITUDE_M, sidelap: float = SIDELAP) -> float:
    footprint = 2 * altitude_m * math.tan(math.radians(CAMERA_HFOV_DEG / 2))
    return footprint * (1 - sidelap)


def components(cells: set[Cell]) -> list[set[Cell]]:
    seen: set[Cell] = set()
    out: list[set[Cell]] = []
    for start in cells:
        if start in seen:
            continue
        comp = {start}
        stack = [start]
        seen.add(start)
        while stack:
            i, j = stack.pop()
            for n in ((i + 1, j), (i - 1, j), (i, j + 1), (i, j - 1)):
                if n in cells and n not in seen:
                    seen.add(n)
                    comp.add(n)
                    stack.append(n)
        out.append(comp)
    return out


def regroup(cells: set[Cell], previous: dict[int, frozenset[Cell]]) -> dict[int, frozenset[Cell]]:
    comps = sorted(components(cells), key=len, reverse=True)
    result: dict[int, frozenset[Cell]] = {}
    claims = []
    for idx, comp in enumerate(comps):
        for gid, old in previous.items():
            overlap = len(comp & old)
            if overlap:
                claims.append((overlap, idx, gid))
    claims.sort(reverse=True)
    taken_ids: set[int] = set()
    assigned: dict[int, int] = {}
    for _, idx, gid in claims:
        if idx in assigned or gid in taken_ids:
            continue
        assigned[idx] = gid
        taken_ids.add(gid)
    for idx, comp in enumerate(comps):
        gid = assigned.get(idx)
        if gid is None:
            gid = 1
            while gid in taken_ids:
                gid += 1
            taken_ids.add(gid)
        result[gid] = frozenset(comp)
    return dict(sorted(result.items()))


def cells_polygon(cells: frozenset[Cell] | set[Cell]) -> BaseGeometry:
    return unary_union([box(i * CELL_M, j * CELL_M, (i + 1) * CELL_M, (j + 1) * CELL_M) for i, j in cells])


@dataclass
class Preview:
    waypoints: list[tuple[float, float]]  # local meters
    length_m: float
    passes: int
    heading_deg: float
    est_time_s: float


def _long_axis_angle(poly: BaseGeometry) -> float:
    rect = poly.minimum_rotated_rectangle
    if rect.geom_type != "Polygon":
        return 0.0
    xs, ys = rect.exterior.coords.xy
    edges = [(xs[k + 1] - xs[k], ys[k + 1] - ys[k]) for k in range(2)]
    dx, dy = max(edges, key=lambda e: math.hypot(*e))
    return math.degrees(math.atan2(dy, dx))


def preview_path(cells: frozenset[Cell] | set[Cell], spacing_m: float | None = None) -> Preview:
    spacing = spacing_m or pass_spacing_m()
    poly = cells_polygon(cells)
    angle = _long_axis_angle(poly)
    origin = poly.centroid
    flat = affinity.rotate(poly, -angle, origin=origin)
    minx, miny, maxx, maxy = flat.bounds
    rows: list[list[tuple[float, float]]] = []
    y = miny + spacing / 2
    while y < maxy:
        hit = flat.intersection(LineString([(minx - 1, y), (maxx + 1, y)]))
        segs = []
        if isinstance(hit, LineString) and not hit.is_empty:
            segs = [hit]
        elif isinstance(hit, MultiLineString):
            segs = list(hit.geoms)
        elif hasattr(hit, "geoms"):
            segs = [g for g in hit.geoms if isinstance(g, LineString)]
        segs = [s for s in segs if s.length >= 1.0]
        if segs:
            pts: list[tuple[float, float]] = []
            for s in sorted(segs, key=lambda s: s.bounds[0]):
                a, b = s.coords[0], s.coords[-1]
                pts.extend([a, b] if a[0] <= b[0] else [b, a])
            rows.append(pts)
        y += spacing
    if not rows:  # tiny group: fly over its center
        c = poly.centroid
        rows = [[(c.x, c.y)]]
    path: list[tuple[float, float]] = []
    for k, row in enumerate(rows):
        path.extend(row if k % 2 == 0 else list(reversed(row)))
    line = LineString(path) if len(path) > 1 else None
    path_back = list(affinity.rotate(line, angle, origin=origin).coords) if line else [(origin.x, origin.y)]
    length = line.length if line else 0.0
    return Preview(
        waypoints=[(round(x, 2), round(y, 2)) for x, y in path_back],
        length_m=length,
        passes=len(rows),
        heading_deg=(90 - angle) % 180,  # compass bearing of the passes
        est_time_s=length / CRUISE_SPEED_MS + max(len(rows) - 1, 0) * TURN_PENALTY_S,
    )


def order_from_dock(starts: dict[int, tuple[float, float]]) -> list[int]:
    """Nearest-neighbor order of groups starting at the dock (0, 0)."""
    here = Point(0, 0)
    remaining = dict(starts)
    order = []
    while remaining:
        gid = min(remaining, key=lambda g: here.distance(Point(remaining[g])))
        order.append(gid)
        here = Point(remaining.pop(gid))
    return order
