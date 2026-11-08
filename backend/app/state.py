"""In-memory mission draft: one selection, its groups and their path previews.

The demo is single-user, so one process-wide state is enough. The selection
(a set of cells) is the source of truth; groups and previews derive from it.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field

from shapely.geometry import LineString, Point

from . import planner
from .course import Course
from .geo import CELL_M, Cell


@dataclass
class MissionState:
    course: Course
    cells: set[Cell] = field(default_factory=set)
    groups: dict[int, frozenset[Cell]] = field(default_factory=dict)
    previews: dict[int, planner.Preview] = field(default_factory=dict)
    version: int = 0
    lock: threading.RLock = field(default_factory=threading.RLock)

    # -- edits ---------------------------------------------------------------
    def edit(self, op: str, cells: set[Cell]) -> int:
        """Apply add/remove/toggle/set. Returns how many cells changed."""
        cells = {c for c in cells if self.course.in_bounds(c)}
        with self.lock:
            before = set(self.cells)
            if op == "add":
                self.cells |= cells
            elif op == "remove":
                self.cells -= cells
            elif op == "toggle":
                self.cells ^= cells
            elif op == "set":
                self.cells = set(cells)
            else:
                raise ValueError(f"unknown op {op!r}")
            changed = len(before ^ self.cells)
            if changed:
                self._regroup()
            return changed

    def clear(self) -> int:
        return self.edit("set", set())

    def _regroup(self) -> None:
        self.groups = planner.regroup(self.cells, self.groups)
        # A preview survives only if its group's cells are unchanged.
        self.previews = {
            gid: p for gid, p in self.previews.items() if gid in self.groups and getattr(p, "_cells", None) == self.groups[gid]
        }
        self.version += 1

    def preview(self, group_ids: list[int] | None = None) -> list[int]:
        with self.lock:
            ids = group_ids if group_ids else list(self.groups)
            done = []
            for gid in ids:
                if gid not in self.groups:
                    continue
                p = planner.preview_path(self.groups[gid])
                p._cells = self.groups[gid]  # type: ignore[attr-defined]
                self.previews[gid] = p
                done.append(gid)
            if done:
                self.version += 1
            return done

    def clear_previews(self) -> None:
        with self.lock:
            if self.previews:
                self.previews = {}
                self.version += 1

    # -- derived -------------------------------------------------------------
    def route(self) -> dict | None:
        """Dock -> each previewed group (nearest first) -> dock, in local meters."""
        with self.lock:
            if not self.previews:
                return None
            order = planner.order_from_dock({g: p.waypoints[0] for g, p in self.previews.items()})
            pts: list[tuple[float, float]] = [(0.0, 0.0)]
            survey = 0.0
            for gid in order:
                pts.extend(self.previews[gid].waypoints)
                survey += self.previews[gid].length_m
            pts.append((0.0, 0.0))
            total = LineString(pts).length
            return {
                "order": order,
                "waypoints": pts,
                "survey_m": survey,
                "total_m": total,
                "transit_m": total - survey,
                "est_time_s": total / planner.CRUISE_SPEED_MS
                + sum(max(self.previews[g].passes - 1, 0) for g in order) * planner.TURN_PENALTY_S,
            }

    def group_stats(self, gid: int) -> dict:
        cells = self.groups[gid]
        poly = planner.cells_polygon(cells)
        minx, miny, maxx, maxy = poly.bounds
        c = poly.centroid
        p = self.previews.get(gid)
        far = Point(0, 0).hausdorff_distance(poly)
        return {
            "id": gid,
            "label": f"Group {gid}",
            "cell_count": len(cells),
            "area_m2": round(len(cells) * CELL_M * CELL_M),
            "size_m": [round(maxx - minx), round(maxy - miny)],
            "holes": self.course.holes_touching(poly),
            "center": self.course.frame.to_lonlat(c.x, c.y),
            "bounds": self.course.lonlat_bounds(poly),
            "farthest_from_dock_m": round(far),
            "preview": None
            if p is None
            else {
                "length_m": round(p.length_m),
                "passes": p.passes,
                "pass_heading_deg": round(p.heading_deg),
                "est_time_s": round(p.est_time_s),
            },
        }

    def snapshot(self, include_cells: bool = True) -> dict:
        with self.lock:
            frame = self.course.frame
            groups = []
            for gid in self.groups:
                g = self.group_stats(gid)
                if include_cells:
                    g["cells"] = sorted(self.groups[gid])
                    p = self.previews.get(gid)
                    g["path"] = [frame.to_lonlat(x, y) for x, y in p.waypoints] if p else None
                groups.append(g)
            r = self.route()
            route = None
            if r:
                route = {
                    "order": r["order"],
                    "total_m": round(r["total_m"]),
                    "survey_m": round(r["survey_m"]),
                    "transit_m": round(r["transit_m"]),
                    "est_time_s": round(r["est_time_s"]),
                }
                if include_cells:
                    route["path"] = [frame.to_lonlat(x, y) for x, y in r["waypoints"]]
            return {
                "version": self.version,
                "total_cells": len(self.cells),
                "total_area_m2": round(len(self.cells) * CELL_M * CELL_M),
                "groups": groups,
                "route": route,
                "assumptions": {
                    "altitude_m": planner.ALTITUDE_M,
                    "sidelap": planner.SIDELAP,
                    "pass_spacing_m": round(planner.pass_spacing_m(), 1),
                    "speed_ms": planner.CRUISE_SPEED_MS,
                },
            }
