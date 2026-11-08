"""Local flat-earth frame centered on the dock.

Everything in the planner works in meters east (x) and north (y) of the dock.
Over the ~1 km extent of the front nine, an equirectangular projection around
the dock is accurate to well under a centimeter per meter, which is far below
the 3 m cell size. The frontend uses the exact same formulas (see
frontend/src/geo.ts), so a cell index means the same square on both sides.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

EARTH_RADIUS_M = 6378137.0
CELL_M = 3.0


@dataclass(frozen=True)
class LocalFrame:
    lat0: float
    lon0: float

    @property
    def _kx(self) -> float:
        return EARTH_RADIUS_M * math.cos(math.radians(self.lat0)) * math.pi / 180.0

    @property
    def _ky(self) -> float:
        return EARTH_RADIUS_M * math.pi / 180.0

    def to_xy(self, lon: float, lat: float) -> tuple[float, float]:
        return ((lon - self.lon0) * self._kx, (lat - self.lat0) * self._ky)

    def to_lonlat(self, x: float, y: float) -> tuple[float, float]:
        return (self.lon0 + x / self._kx, self.lat0 + y / self._ky)


Cell = tuple[int, int]


def cell_of(x: float, y: float) -> Cell:
    return (math.floor(x / CELL_M), math.floor(y / CELL_M))


def cell_center(c: Cell) -> tuple[float, float]:
    return ((c[0] + 0.5) * CELL_M, (c[1] + 0.5) * CELL_M)
