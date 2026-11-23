"""Fly a divot task queue on ArduPilot SITL from the command line, headless.

    cd backend
    uv run python -m tools.fly_queue --divots 20 --seed 1
    uv run python -m tools.fly_queue --divots 8 --holes 1 2 3 --charge-speedup 1000
    uv run python -m tools.fly_queue --divots 8 --scan-holes 1 --charge-speedup 1000

With --scan-holes the divots are placed hidden on those holes' fairways, the
agent's plan_scan tool plans a scan of the same holes, the scan flies, and the
queue repairs only what the scan found (the scan-then-repair flow).

It generates fake divots, plans legs with charge stops, and flies every leg on
SITL through the same TaskQueue and vehicle link the web app uses. Charging is
simulated (a wait, then MAV_CMD_BATTERY_RESET). It prints progress, then a JSON
report. Exit code 0 means every task is done, every leg landed on the dock,
and the queue needed as many legs as planned.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import logging
import sys
import time

from app import config
from app.course import Course
from app.agent import Agent
from app.energy import EnergyModel
from app.scan import DivotField
from app.sitl_drone import SitlDrone
from app.state import MissionState
from app.task_queue import TaskQueue

LAND_TOLERANCE_M = 3.0


async def run(args) -> dict:
    course = Course()
    model = EnergyModel.from_env()
    if args.charge_speedup:
        model = dataclasses.replace(model, charge_speedup=args.charge_speedup)
    drone = SitlDrone(course.frame, args.connection)
    await drone.on_startup()
    deadline = time.monotonic() + 60
    while not drone.connected:
        if time.monotonic() > deadline:
            raise SystemExit(f"No MAVLink heartbeat from {args.connection} after 60 s. Is SITL running?")
        await asyncio.sleep(0.5)
    q = TaskQueue(course, drone, model)
    scan_report = None
    if args.scan_holes:
        field = DivotField(course, q, drone, seed=args.seed)
        field.scatter(args.divots, args.scan_holes, args.seed)
        state = MissionState(course)
        plan = Agent(course, state, q, field).run_tool("plan_scan", {"holes": args.scan_holes}, [])
        print(f"Scan plan: {plan}", flush=True)
        if "error" in plan:
            raise SystemExit(plan["error"])
        field.start_scan(state.route()["waypoints"])
        t0 = time.monotonic()
        while field.scanning:
            tel = drone.telemetry()
            print(f"[{time.monotonic() - t0:5.0f}s] scan     {tel['mode']:<9} alt {tel['alt_m']:5.1f} m  found {field.snapshot()['found']}  {tel['message']}", flush=True)
            await asyncio.sleep(2.0)
        scan_report = {k: v for k, v in field.snapshot().items() if k != "placed"}
        scan_report["fits_one_battery"] = plan.get("fits_one_battery")
        print(f"Scan result: {scan_report}", flush=True)
        if not q.tasks:
            raise SystemExit("The scan found nothing to repair")
        await q._top_up("after the scan")
    else:
        q.generate(args.divots, args.holes, args.seed, service_s=args.service_s)
    plan0 = q.snapshot()
    planned_legs = len(plan0["legs"])
    print(f"{len(plan0['tasks'])} divots, planned {planned_legs} legs:", flush=True)
    for leg in plan0["legs"]:
        print(f"  leg {leg['index']}: {len(leg['task_ids'])} tasks, {leg['distance_m']} m, {leg['time_s']} s, "
              f"{leg['mah']} mAh ({leg['pct_of_pack']}% of pack, lands at {leg['end_pct']}%)", flush=True)  # fmt: skip
    q.start()
    t0 = time.monotonic()
    last = ""
    while q.running:
        tel = drone.telemetry()
        line = f"{q.phase:<8} {tel['mode']:<9} alt {tel['alt_m']:5.1f} m  batt {tel['battery_pct']:5.1f}%  {q.message}"
        if line != last:
            print(f"[{time.monotonic() - t0:5.0f}s] {line}", flush=True)
            last = line
        if time.monotonic() - t0 > args.timeout:
            q.abort()
            raise SystemExit(f"Timed out after {args.timeout:.0f} s: {q.message}")
        await asyncio.sleep(1.0)
    s = q.snapshot()
    report = {
        "phase": s["phase"],
        "message": s["message"],
        "counts": s["counts"],
        "planned_legs": planned_legs,
        "legs": [
            {k: leg[k] for k in ("index", "status", "task_ids", "distance_m", "time_s", "mah", "end_pct", "charge_after_s", "actual")}
            for leg in s["legs"]
        ],
        "energy_model": {k: s["energy"][k] for k in ("pack_mah", "reserve_pct", "margin", "hover_a", "cruise_a", "calibrated")},
        "wall_s": round(time.monotonic() - t0),
        "scan": scan_report,
    }
    report["ok"] = (
        s["phase"] == "done"
        and s["counts"]["done"] == len(s["tasks"])
        and len(s["legs"]) == planned_legs
        and all(leg["status"] == "done" for leg in s["legs"])
        and all(leg["actual"].get("landed_from_dock_m") is not None and leg["actual"]["landed_from_dock_m"] <= LAND_TOLERANCE_M for leg in s["legs"])
        and not drone.v["armed"]
    )
    drone.close()
    return report


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--divots", type=int, default=20)
    ap.add_argument("--holes", type=int, nargs="*", default=None)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--scan-holes", type=int, nargs="*", default=None, help="place the divots hidden on these holes and find them with a scan first")
    ap.add_argument("--service-s", type=float, default=15.0)
    ap.add_argument("--charge-speedup", type=float, default=None, help="override CHARGE_SPEEDUP for a quick run")
    ap.add_argument("--connection", default=config.SITL_CONNECTION)
    ap.add_argument("--timeout", type=float, default=3600, help="seconds of wall time before giving up")
    args = ap.parse_args()
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    report = asyncio.run(run(args))
    print(json.dumps(report, indent=2))
    sys.exit(0 if report["ok"] else 1)


if __name__ == "__main__":
    main()
