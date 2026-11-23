"""Point tasks for the drone to visit, starting with fake golf divots.

A task is a spot on the course that the drone flies to, works at for a while
(descend, hold, climb back), and marks done. In simulation the work is a
hover; on the real drone it would be whatever the payload does (inspect,
photograph, fill). Divots are generated at random inside the OSM fairways so
there is a realistic queue to plan without field data.

Status: pending -> active -> done, or failed. A task goes back to pending if
its leg ends early (abort, low battery), and fails after MAX_ATTEMPTS, or at
once if it can't be reached on one battery.
"""

from __future__ import annotations

import random
from dataclasses import asdict, dataclass
from typing import Literal

from shapely.geometry import Point
from shapely.ops import unary_union

from .course import Course

Status = Literal["pending", "active", "done", "failed"]
MAX_ATTEMPTS = 2
MIN_SPACING_M = 6.0  # two divots closer than this would be one stop


@dataclass
class Task:
    id: str
    kind: str
    x: float
    y: float
    hole: int | None
    service_s: float
    status: Status = "pending"
    attempts: int = 0
    leg: int | None = None  # the leg that flew it (done) or will fly it next (pending)
    note: str = ""

    def to_json(self, course: Course) -> dict:
        lon, lat = course.frame.to_lonlat(self.x, self.y)
        d = asdict(self)
        d.update(x=round(self.x, 2), y=round(self.y, 2), lon=lon, lat=lat)
        return d


def generate_divots(
    course: Course,
    count: int,
    holes: list[int] | None = None,
    seed: int | None = None,
    service_s: float = 15.0,
    start_index: int = 1,
) -> list[Task]:
    """`count` fake divots at random spots inside the fairways of `holes`
    (all holes with a fairway if None), spread by fairway area."""
    rng = random.Random(seed)
    ids = [h for h in (holes or sorted(course.holes)) if h in course.holes and course.holes[h].fairway is not None]
    if not ids:
        raise ValueError("None of those holes has a fairway in the course data")
    areas = unary_union([course.holes[h].fairway for h in ids])
    minx, miny, maxx, maxy = areas.bounds
    pts: list[tuple[float, float]] = []
    tries = 0
    while len(pts) < count and tries < count * 500:
        tries += 1
        x, y = rng.uniform(minx, maxx), rng.uniform(miny, maxy)
        if not areas.contains(Point(x, y)):
            continue
        if any((x - a) ** 2 + (y - b) ** 2 < MIN_SPACING_M**2 for a, b in pts):
            continue
        pts.append((x, y))
    out = []
    for k, (x, y) in enumerate(pts):
        hole = next((h for h in ids if course.holes[h].fairway.contains(Point(x, y))), None)
        out.append(Task(id=f"D{start_index + k:02d}", kind="divot", x=x, y=y, hole=hole, service_s=service_s))
    return out
