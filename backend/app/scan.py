"""Find divots from the air: hidden divots you place, and a simulated camera scan.

The flow this supports:

1. You place divots on the map (or scatter random ones). They are the ground
   truth of the simulation and the planner does not see them.
2. A **scan run** flies a coverage route (the same route the Fly button
   flies). While it flies, any placed divot that passes under the camera
   footprint at survey altitude gets one detection roll. A hit becomes a
   pending task at the detected position (the true one plus a small error).
3. A **repair run** is the task queue: it flies the found tasks in legs with
   charge stops (task_queue.py).

The "camera" is geometry only: a strip as wide as the camera's ground
footprint (planner.CAMERA_HFOV_DEG at the current altitude) under the drone,
a detection probability, and a position error. There is no image, resolution
or lighting model. The numbers are placeholders like the rest of the sim.
"""

from __future__ import annotations

import asyncio
import math
import os
import random
from dataclasses import asdict, dataclass

from shapely.geometry import LineString, Point

from . import planner, vitals
from .course import Course

DETECT_P = float(os.getenv("SCAN_DETECT_P", "0.9"))  # chance a divot under the footprint is found
POS_ERR_M = float(os.getenv("SCAN_POS_ERR_M", "0.5"))  # 1-sigma error of the detected position, per axis
MIN_ALT_FRAC = 0.8  # only detect at 80% or more of survey altitude (not on takeoff or landing)
POLL_S = 0.2


def footprint_m(alt_m: float) -> float:
    """Width of ground the camera sees across track at this altitude."""
    return 2 * max(alt_m, 0.0) * math.tan(math.radians(planner.CAMERA_HFOV_DEG / 2))


@dataclass
class Placed:
    """A divot that really exists in the simulation. Hidden from the planner."""

    id: str
    x: float
    y: float
    hole: int | None
    found_task: str | None = None  # the task a scan created for it
    found_error_m: float | None = None  # detected position vs. the real one
    seen: bool = False  # was under the camera during a scan
    missed: int = 0  # scans that saw it and failed the detection roll

    def to_json(self, course: Course) -> dict:
        lon, lat = course.frame.to_lonlat(self.x, self.y)
        d = asdict(self)
        d.update(x=round(self.x, 2), y=round(self.y, 2), lon=lon, lat=lat)
        if d["found_error_m"] is not None:
            d["found_error_m"] = round(d["found_error_m"], 2)
        return d


class DivotField:
    """The placed divots and the scanner that looks for them."""

    def __init__(self, course: Course, queue, link, seed: int | None = None) -> None:
        self.course = course
        self.queue = queue  # task_queue.TaskQueue
        self.link = link
        self.rng = random.Random(seed)
        self.placed: dict[str, Placed] = {}
        self._next = 1
        self.scanning = False
        self.scan_count = 0
        self.last_scan: dict | None = None
        self._task: asyncio.Task | None = None

    # -- placing ---------------------------------------------------------------------
    def _hole_at(self, x: float, y: float) -> int | None:
        p = Point(x, y)
        return next((h for h, hole in self.course.holes.items() if hole.fairway is not None and hole.fairway.contains(p)), None)

    def add(self, points_xy: list[tuple[float, float]]) -> list[Placed]:
        out = []
        for x, y in points_xy:
            d = Placed(f"P{self._next:02d}", x, y, self._hole_at(x, y))
            self._next += 1
            self.placed[d.id] = d
            out.append(d)
        self.queue._changed()
        return out

    def scatter(self, count: int, holes: list[int] | None = None, seed: int | None = None) -> list[Placed]:
        from .tasks import generate_divots

        pts = generate_divots(self.course, count, holes, seed)
        return self.add([(t.x, t.y) for t in pts])

    def remove(self, ids: list[str]) -> None:
        for i in ids:
            self.placed.pop(i, None)
        self.queue._changed()

    def clear(self) -> None:
        self.placed = {}
        self._next = 1
        self.last_scan = None
        self.queue._changed()

    # -- scanning ----------------------------------------------------------------------
    def start_scan(self, waypoints: list[tuple[float, float]]) -> None:
        """Fly the survey route with the camera on."""
        if self.scanning:
            raise RuntimeError("Already scanning")
        if self.queue.running:
            raise RuntimeError("The task queue is running. Abort it first.")
        if self.link.flying:
            raise RuntimeError("The drone is already flying")
        self.scanning = True
        self.scan_count += 1
        self._task = asyncio.create_task(self._watch(waypoints))

    async def _watch(self, waypoints: list[tuple[float, float]]) -> None:
        rolled: set[str] = set()
        found: list[str] = []
        missed: list[str] = []
        last: tuple[float, float] | None = None
        alt_min = MIN_ALT_FRAC * planner.ALTITUDE_M
        started = False
        frame = self.course.frame
        try:
            # A scan, like every leg, starts on a full pack.
            await self.queue._top_up("before the scan")
            if self.queue.phase == "charging":
                self.queue.phase, self.queue.message = "idle", "Charged. Scanning."
            try:
                self.link.start(waypoints)
            except RuntimeError as e:
                self._log(f"Scan could not start: {e}", vitals.ERROR)
                return
            while True:
                flying = self.link.flying
                started = started or flying
                if started and not flying:
                    break
                tel = self.link.telemetry()
                here = frame.to_xy(tel["lon"], tel["lat"])
                if tel.get("armed") and tel["alt_m"] >= alt_min:
                    half = footprint_m(tel["alt_m"]) / 2
                    seg = LineString([last, here]) if last and last != here else Point(here)
                    for d in self.placed.values():
                        if d.id in rolled or self._found(d):
                            continue
                        if seg.distance(Point(d.x, d.y)) <= half:
                            rolled.add(d.id)
                            d.seen = True
                            if self.rng.random() < DETECT_P:
                                tx = d.x + self.rng.gauss(0, POS_ERR_M)
                                ty = d.y + self.rng.gauss(0, POS_ERR_M)
                                task = self.queue.add_found(tx, ty, d.hole, note=f"Found by scan {self.scan_count}")
                                d.found_task = task.id
                                d.found_error_m = math.dist((tx, ty), (d.x, d.y))
                                found.append(d.id)
                                self._log(f"Scan: divot found near hole {d.hole or '?'} ({task.id})")
                            else:
                                d.missed += 1
                                missed.append(d.id)
                    last = here
                else:
                    last = None
                await asyncio.sleep(POLL_S)
        finally:
            self.scanning = False
            never = [d.id for d in self.placed.values() if d.id not in rolled and not self._found(d)]
            self.last_scan = {"index": self.scan_count, "found": len(found), "missed_in_view": len(missed), "not_covered": len(never)}
            self._log(
                f"Scan {self.scan_count} done: {len(found)} found, {len(missed)} under the camera but missed, "
                f"{len(never)} never under the camera",
                vitals.NOTICE,
            )
            self.queue.replan()

    def _found(self, d: Placed) -> bool:
        """Found by a scan, and the task still exists (clearing the queue forgets finds)."""
        if d.found_task and d.found_task not in self.queue.tasks:
            d.found_task, d.found_error_m = None, None
        return d.found_task is not None

    def _log(self, text: str, severity: int = vitals.INFO) -> None:
        msglog = getattr(self.link, "msglog", None)
        if msglog is not None:
            msglog.add(text, severity, "planner")

    def snapshot(self) -> dict:
        ps = list(self.placed.values())
        for d in ps:
            self._found(d)
        errs = [d.found_error_m for d in ps if d.found_error_m is not None]
        return {
            "placed": [d.to_json(self.course) for d in ps],
            "scanning": self.scanning,
            "scans": self.scan_count,
            "last_scan": self.last_scan,
            "found": sum(1 for d in ps if d.found_task),
            "not_found": sum(1 for d in ps if not d.found_task),
            "mean_error_m": round(sum(errs) / len(errs), 2) if errs else None,
            "camera": {
                "footprint_m": round(footprint_m(planner.ALTITUDE_M), 1),
                "detect_p": DETECT_P,
                "pos_err_m": POS_ERR_M,
            },
        }
