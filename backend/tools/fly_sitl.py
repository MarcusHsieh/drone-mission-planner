"""Fly a planner mission on ArduPilot SITL from the command line, headless.

    cd backend
    uv run python -m tools.fly_sitl --hole 2
    uv run python -m tools.fly_sitl --hole 4 --feature fairway --connection tcp:127.0.0.1:5760

It selects a hole's feature the same way the agent's select_area tool does,
plans the route with the planner, uploads it to SITL, flies it, and prints a
report: which waypoints the autopilot reported reached, how far the flown
track strayed from the planned route, and where it landed. Exit code 0 means
every waypoint was reached and it landed at the dock.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import sys
import time

from shapely.geometry import LineString, Point

from app import config
from app.course import Course
from app.mavlink_mission import nav_seqs
from app.sitl_drone import SitlDrone
from app.state import MissionState

LAND_TOLERANCE_M = 3.0


def plan_route(course: Course, hole: int, feature: str) -> dict:
    state = MissionState(course)
    state.edit("add", course.feature_cells(hole, feature, 30.0))
    state.preview()
    route = state.route()
    if route is None:
        raise SystemExit(f"Nothing to fly: hole {hole} has no {feature} cells")
    return route


def fly(hole: int, feature: str, connection: str, timeout_s: float, quiet: bool = False) -> dict:
    course = Course()
    route = plan_route(course, hole, feature)
    drone = SitlDrone(course.frame, connection)
    deadline = time.monotonic() + 60
    while not drone.connected:
        if time.monotonic() > deadline:
            raise SystemExit(f"No MAVLink heartbeat from {connection} after 60 s. Is SITL running?")
        time.sleep(0.5)
    drone.start(route["waypoints"])
    t0 = time.monotonic()
    last = ""
    while not drone.wait_done(1.0):
        tel = drone.telemetry()
        line = f"[{time.monotonic() - t0:5.0f}s] {tel['mode']:<9} alt {tel['alt_m']:5.1f} m  {tel['message']}"
        if not quiet and line[9:] != last:
            print(line, flush=True)
            last = line[9:]
        if time.monotonic() - t0 > timeout_s:
            drone.abort()
            raise SystemExit(f"Timed out after {timeout_s:.0f} s: {tel['message']}")

    planned = LineString(route["waypoints"])
    track = drone.track
    max_dev = max((planned.distance(Point(p)) for p in track), default=math.inf)
    land_xy = drone.frame.to_xy(drone.v["lon"], drone.v["lat"])
    expected = nav_seqs(drone.items)
    report = {
        "hole": hole,
        "feature": feature,
        "phase": drone.phase,
        "message": drone.message,
        "mission_items": len(drone.items),
        "waypoints_expected": len(expected),
        "waypoints_reached": len(drone.reached),
        "missed": sorted(set(expected) - set(drone.reached)),
        "reached_in_order": drone.reached == expected,
        "route_m": round(route["total_m"]),
        "flown_m": round(drone.flown_m),
        "max_deviation_m": round(max_dev, 1),
        "landed_from_dock_m": round(math.hypot(*land_xy), 2),
        "flight_time_s": drone.telemetry()["elapsed_s"],
        "armed": drone.v["armed"],
    }
    report["ok"] = (
        drone.phase == "DONE"
        and not report["missed"]
        and report["reached_in_order"]
        and not drone.v["armed"]
        and report["landed_from_dock_m"] <= LAND_TOLERANCE_M
    )
    return report


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--hole", type=int, default=2)
    ap.add_argument("--feature", default="fairway", choices=["fairway", "green", "tees", "corridor"])
    ap.add_argument("--connection", default=config.SITL_CONNECTION)
    ap.add_argument("--timeout", type=float, default=1200, help="seconds of wall time before giving up")
    args = ap.parse_args()
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    report = fly(args.hole, args.feature, args.connection, args.timeout)
    print(json.dumps(report, indent=2))
    sys.exit(0 if report["ok"] else 1)


if __name__ == "__main__":
    main()
