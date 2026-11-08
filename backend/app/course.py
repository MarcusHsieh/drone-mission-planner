"""San Ramon Golf Club front nine, built from an OpenStreetMap snapshot.

The snapshot (app/data/osm_front9.json) was read from the OSM API on
2026-11-08 for relation 3566976 and trimmed to features near holes 1-9.
Each entry is [kind, ref, par, osm_id, rings]; each ring is
"role|x0,y0;dx,dy;..." with coordinates in micro-degrees, delta encoded.

OSM fairways carry no hole number, so each fairway is assigned to the hole
whose center line runs through it the most. Greens and tees are assigned to
the hole whose line ends / starts nearest to them.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import shapely
from shapely.geometry import LineString, Point, Polygon, mapping
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union

from .geo import CELL_M, Cell, LocalFrame

DATA_FILE = Path(__file__).parent / "data" / "osm_front9.json"

# Dock: back tee of hole 1 (OSM way 934037995), centroid. Confirmed against the
# red dot in mj's screenshots.
DOCK_LAT = 37.733897
DOCK_LON = -121.929056
DOCK_OSM_WAY = "w934037995"

# Corridor width used when a hole has no mapped fairway, or when asked for one.
DEFAULT_CORRIDOR_M = 40.0


def _decode_ring(enc: str) -> tuple[str, list[tuple[float, float]]]:
    role, data = enc.split("|", 1)
    x = y = 0
    pts: list[tuple[float, float]] = []
    for pair in data.split(";"):
        dx, dy = pair.split(",")
        x += int(dx)
        y += int(dy)
        pts.append((x / 1e6, y / 1e6))
    return role, pts


@dataclass
class Hole:
    ref: int
    par: int
    line: LineString  # local meters, tee -> green
    fairway: BaseGeometry | None = None
    green: BaseGeometry | None = None
    tees: list[BaseGeometry] = field(default_factory=list)

    @property
    def length_m(self) -> float:
        return self.line.length

    def area(self, feature: str, corridor_m: float = DEFAULT_CORRIDOR_M) -> BaseGeometry | None:
        if feature == "fairway":
            return self.fairway
        if feature == "green":
            return self.green
        if feature == "tees":
            return unary_union(self.tees) if self.tees else None
        if feature == "corridor":
            return self.line.buffer(corridor_m / 2.0, cap_style="flat")
        raise ValueError(f"unknown feature {feature!r}")


@dataclass
class OtherFeature:
    kind: str
    osm_id: str
    geom: BaseGeometry  # local meters
    hole: int | None = None


class Course:
    def __init__(self, data_file: Path = DATA_FILE) -> None:
        self.name = "San Ramon Golf Club (front nine)"
        self.osm_relation = 3566976
        self.frame = LocalFrame(DOCK_LAT, DOCK_LON)
        self.holes: dict[int, Hole] = {}
        self.others: list[OtherFeature] = []
        self._load(json.loads(data_file.read_text()))
        self._assign()
        # Grid extent: everything within 60 m of a front-nine feature.
        everything = unary_union([h.line for h in self.holes.values()] + [o.geom for o in self.others])
        minx, miny, maxx, maxy = everything.buffer(60).bounds
        self.cell_bounds = (
            int(np.floor(minx / CELL_M)),
            int(np.floor(miny / CELL_M)),
            int(np.ceil(maxx / CELL_M)),
            int(np.ceil(maxy / CELL_M)),
        )

    # -- loading -----------------------------------------------------------
    def _xy(self, pts: list[tuple[float, float]]) -> list[tuple[float, float]]:
        return [self.frame.to_xy(lon, lat) for lon, lat in pts]

    def _load(self, rows: list) -> None:
        for kind, ref, par, osm_id, rings in rows:
            decoded = [(role, self._xy(pts)) for role, pts in (_decode_ring(r) for r in rings)]
            if kind == "hole":
                if not ref or int(ref) > 9:
                    continue
                self.holes[int(ref)] = Hole(int(ref), int(par or 0), LineString(decoded[0][1]))
                continue
            outers = [Polygon(p) for role, p in decoded if role in ("", "outer") and len(p) >= 4]
            inners = [Polygon(p) for role, p in decoded if role == "inner" and len(p) >= 4]
            if not outers:
                continue
            geom = unary_union([o.buffer(0) for o in outers])
            if inners:
                geom = geom.difference(unary_union([i.buffer(0) for i in inners]))
            self.others.append(OtherFeature(kind, osm_id, geom))

    def _assign(self) -> None:
        fairways: dict[int, list[BaseGeometry]] = {r: [] for r in self.holes}
        for f in self.others:
            if f.kind == "fairway":
                best, best_len = None, 0.0
                for ref, h in self.holes.items():
                    inside = h.line.intersection(f.geom.buffer(5)).length
                    if inside > best_len:
                        best, best_len = ref, inside
                if best is not None and best_len >= 15:
                    fairways[best].append(f.geom)
                    f.hole = best
            elif f.kind == "green":
                ref, d = self._nearest(f.geom, lambda h: Point(h.line.coords[-1]))
                if d <= 25 and self.holes[ref].green is None:
                    self.holes[ref].green = f.geom
                    f.hole = ref
            elif f.kind == "tee":
                ref, d = self._nearest(f.geom, lambda h: Point(h.line.coords[0]))
                if d <= 60:
                    self.holes[ref].tees.append(f.geom)
                    f.hole = ref
        for ref, parts in fairways.items():
            if parts:
                self.holes[ref].fairway = unary_union(parts)

    def _nearest(self, geom: BaseGeometry, anchor) -> tuple[int, float]:
        best, best_d = -1, float("inf")
        for ref, h in self.holes.items():
            d = geom.distance(anchor(h))
            if d < best_d:
                best, best_d = ref, d
        return best, best_d

    # -- queries -------------------------------------------------------------
    def cells_in(self, geom: BaseGeometry) -> set[Cell]:
        """Cells whose center falls inside geom (and inside the grid extent)."""
        if geom is None or geom.is_empty:
            return set()
        minx, miny, maxx, maxy = geom.bounds
        i0, j0 = int(np.floor(minx / CELL_M)), int(np.floor(miny / CELL_M))
        i1, j1 = int(np.ceil(maxx / CELL_M)), int(np.ceil(maxy / CELL_M))
        ii, jj = np.meshgrid(np.arange(i0, i1 + 1), np.arange(j0, j1 + 1), indexing="ij")
        cx, cy = (ii + 0.5) * CELL_M, (jj + 0.5) * CELL_M
        shapely.prepare(geom)
        mask = shapely.contains_xy(geom, cx, cy)
        bi0, bj0, bi1, bj1 = self.cell_bounds
        mask &= (ii >= bi0) & (ii <= bi1) & (jj >= bj0) & (jj <= bj1)
        return set(zip(ii[mask].tolist(), jj[mask].tolist()))

    def in_bounds(self, c: Cell) -> bool:
        bi0, bj0, bi1, bj1 = self.cell_bounds
        return bi0 <= c[0] <= bi1 and bj0 <= c[1] <= bj1

    def holes_touching(self, geom: BaseGeometry) -> list[int]:
        out = []
        for ref, h in sorted(self.holes.items()):
            if h.fairway is not None and h.fairway.intersection(geom).area > 20:
                out.append(ref)
            elif h.line.intersection(geom).length > 10:
                out.append(ref)
        return out

    def to_lonlat_geom(self, geom: BaseGeometry) -> BaseGeometry:
        def inv(xy: np.ndarray) -> np.ndarray:
            lon, lat = self.frame.to_lonlat(xy[:, 0], xy[:, 1])
            return np.column_stack([np.round(lon, 7), np.round(lat, 7)])

        return shapely.transform(geom, inv)

    def geojson(self) -> dict:
        feats = []
        for o in self.others:
            feats.append({
                "type": "Feature",
                "properties": {"kind": o.kind, "osm": o.osm_id, "hole": o.hole},
                "geometry": mapping(self.to_lonlat_geom(o.geom)),
            })
        for ref, h in sorted(self.holes.items()):
            feats.append({
                "type": "Feature",
                "properties": {"kind": "hole", "hole": ref, "par": h.par, "label": str(ref)},
                "geometry": mapping(self.to_lonlat_geom(h.line)),
            })
            tee = self.to_lonlat_geom(Point(h.line.coords[0]))
            feats.append({
                "type": "Feature",
                "properties": {"kind": "hole_label", "hole": ref, "label": f"{ref}"},
                "geometry": mapping(tee),
            })
        return {"type": "FeatureCollection", "features": feats}

    def hole_summary(self) -> list[dict]:
        out = []
        for ref, h in sorted(self.holes.items()):
            fw = h.fairway
            out.append({
                "hole": ref,
                "par": h.par,
                "length_m": round(h.length_m),
                "has_fairway": fw is not None,
                "fairway_area_m2": round(fw.area) if fw is not None else 0,
                "fairway_cells": len(self.cells_in(fw)) if fw is not None else 0,
                "has_green": h.green is not None,
                "tee_count": len(h.tees),
            })
        return out

    def lonlat_bounds(self, geom: BaseGeometry) -> list[float]:
        minx, miny, maxx, maxy = geom.bounds
        lon0, lat0 = self.frame.to_lonlat(minx, miny)
        lon1, lat1 = self.frame.to_lonlat(maxx, maxy)
        return [lon0, lat0, lon1, lat1]

    def feature_cells(self, hole: int, feature: str, corridor_m: float = DEFAULT_CORRIDOR_M, min_piece_cells: int = 10) -> set[Cell]:
        """Cells for one hole's feature, minus slivers.

        OSM fairways are often cut around the green, which leaves thin collars
        that rasterize into a handful of 1-4 cell islands. Pieces smaller than
        min_piece_cells are dropped so a feature selection reads as one or two
        clean groups. Greens and tees are small, so they keep every piece.
        """
        from .planner import components  # local import to avoid a cycle

        h = self.holes[hole]
        cells = self.cells_in(h.area(feature, corridor_m))
        if feature in ("green", "tees"):
            return cells
        return set().union(*[c for c in components(cells) if len(c) >= min_piece_cells]) if cells else set()
