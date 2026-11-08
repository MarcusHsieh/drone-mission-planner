"""Simulated drone for M1 (ArduPilot SITL replaces this later).

Flies the previewed route: take off at the dock, fly every group's passes in
order, return, land. Publishes telemetry shaped like what we'll read from
MAVLink (position, altitude, heading, speed, battery, mode, GPS) at 5 Hz.
Battery and voltage use a made-up linear model; the UI labels them simulated.
"""

from __future__ import annotations

import asyncio
import math
import time
from typing import Any

from . import config
from .geo import LocalFrame

TICK_S = 0.2  # 5 Hz
CLIMB_MS = 2.5
CRUISE_MS = 5.0
ALT_M = 15.0
ENDURANCE_S = 20 * 60  # placeholder: full battery = 20 min of flight
CELLS = 4  # 4S pack
V_FULL, V_EMPTY = 4.2 * CELLS, 3.5 * CELLS


class DroneSim:
    def __init__(self, frame: LocalFrame) -> None:
        self.frame = frame
        self.subscribers: set[asyncio.Queue] = set()
        self.task: asyncio.Task | None = None
        self.abort_requested = False
        self._reset()

    def _reset(self) -> None:
        self.x = self.y = 0.0
        self.alt = 0.0
        self.heading = 0.0
        self.speed = 0.0
        self.battery = 100.0
        self.mode = "DOCKED"
        self.flown_m = 0.0
        self.route_m = 0.0
        self.t0 = None
        self.sim_elapsed = 0.0
        self.message = "On dock, charged"

    @property
    def flying(self) -> bool:
        return self.task is not None and not self.task.done()

    def telemetry(self) -> dict[str, Any]:
        lon, lat = self.frame.to_lonlat(self.x, self.y)
        pct = max(self.battery, 0.0)
        return {
            "t": time.time(),
            "lat": lat,
            "lon": lon,
            "alt_m": round(self.alt, 1),
            "heading_deg": round(self.heading % 360),
            "groundspeed_ms": round(self.speed, 1),
            "battery_pct": round(pct, 1),
            "voltage_v": round(V_EMPTY + (V_FULL - V_EMPTY) * pct / 100, 2),
            "mode": self.mode,
            "gps_fix": "3D",
            "satellites": 14,
            "distance_flown_m": round(self.flown_m),
            "route_m": round(self.route_m),
            "progress_pct": round(100 * self.flown_m / self.route_m) if self.route_m else 0,
            "elapsed_s": round(self.sim_elapsed),
            "speedup": config.SIM_SPEEDUP,
            "message": self.message,
            "simulated": True,
        }

    async def publish(self) -> None:
        msg = self.telemetry()
        for q in list(self.subscribers):
            if q.full():
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            q.put_nowait(msg)

    def start(self, waypoints: list[tuple[float, float]]) -> None:
        if self.flying:
            raise RuntimeError("Already flying")
        self._reset()
        self.abort_requested = False
        self.route_m = sum(math.dist(a, b) for a, b in zip(waypoints, waypoints[1:]))
        self.task = asyncio.create_task(self._fly(waypoints))

    def abort(self) -> None:
        self.abort_requested = True

    async def _step(self, dt_sim: float) -> None:
        self.sim_elapsed += dt_sim
        self.battery -= 100 * dt_sim / ENDURANCE_S
        await self.publish()
        await asyncio.sleep(TICK_S)

    async def _fly(self, waypoints: list[tuple[float, float]]) -> None:
        dt = TICK_S * config.SIM_SPEEDUP
        try:
            self.mode, self.message = "TAKEOFF", "Taking off from dock"
            while self.alt < ALT_M:
                self.alt = min(ALT_M, self.alt + CLIMB_MS * dt)
                self.speed = 0.0
                await self._step(dt)
            self.mode, self.message = "AUTO", "Surveying"
            path = list(waypoints)
            k = 1
            while k < len(path):
                if self.abort_requested and self.mode != "RTL":
                    self.mode, self.message = "RTL", "Abort: returning to dock"
                    path = [(self.x, self.y), (0.0, 0.0)]
                    k = 1
                    continue
                tx, ty = path[k]
                d = math.dist((self.x, self.y), (tx, ty))
                step = CRUISE_MS * dt
                if d > 0.01:
                    self.heading = math.degrees(math.atan2(tx - self.x, ty - self.y))
                if d <= step:
                    self.flown_m += d
                    self.x, self.y = tx, ty
                    k += 1
                    if k == len(path) - 1 and self.mode == "AUTO":
                        self.mode, self.message = "RTL", "Survey done, returning to dock"
                    if d < step * 0.5:
                        continue  # don't burn a tick on a tiny leg
                else:
                    self.x += (tx - self.x) / d * step
                    self.y += (ty - self.y) / d * step
                    self.flown_m += step
                self.speed = CRUISE_MS
                await self._step(dt)
            self.mode, self.message, self.speed = "LAND", "Landing on dock", 0.0
            while self.alt > 0:
                self.alt = max(0.0, self.alt - CLIMB_MS * dt)
                await self._step(dt)
            self.mode = "DOCKED"
            self.message = "Aborted and docked" if self.abort_requested else "Mission complete, charging on dock"
            await self.publish()
        except asyncio.CancelledError:
            self.mode, self.message = "DOCKED", "Simulation stopped"
            raise
