"""Simulated drone for M1 (ArduPilot SITL replaces this later).

Flies the previewed route: take off at the dock, fly every group's passes in
order, return, land. Publishes telemetry shaped like what we'll read from
MAVLink (position, altitude, heading, speed, battery, mode, GPS) at 5 Hz.
Battery and voltage use a made-up linear model; the UI labels them simulated.
The dashboard's health panels (EKF, vibration, sensors, link) get steady
"all good" values so the layout can be tried without SITL; only SITL's are real.
"""

from __future__ import annotations

import asyncio
import math
import time
from typing import Any

from . import config
from . import vitals
from .geo import LocalFrame

TICK_S = 0.2  # 5 Hz
CLIMB_MS = 2.5
CRUISE_MS = 5.0
ALT_M = 15.0
ENDURANCE_S = 20 * 60  # placeholder: full battery = 20 min of flight
CELLS = 6  # the airframe's 6S pack
V_FULL, V_EMPTY = 4.2 * CELLS, 3.5 * CELLS
# Placeholder capacity, the same as BATT_CAPACITY in sitl/planner.parm, so a
# 20 minute flight draws a plausible 36 A. The real pack is 2200 mAh.
CAPACITY_MAH = 12000
HOVER_A = CAPACITY_MAH / 1000 / (ENDURANCE_S / 3600)
IDLE_A = 0.4


class DroneSim:
    def __init__(self, frame: LocalFrame) -> None:
        self.frame = frame
        self.subscribers: set[asyncio.Queue] = set()
        self.task: asyncio.Task | None = None
        self.abort_requested = False
        self.msglog = vitals.MessageLog()
        self.msglog.add("Built-in simulator ready (health values are placeholders)", vitals.INFO, "planner")
        self._reset()

    def _reset(self) -> None:
        self.x = self.y = 0.0
        self.alt = 0.0
        self.heading = 0.0
        self.speed = 0.0
        self.climb = 0.0
        self.wp_dist = 0.0
        self.battery = 100.0
        self.mode = "DOCKED"
        self.flown_m = 0.0
        self.route_m = 0.0
        self.t0 = None
        self.sim_elapsed = 0.0
        self.message = "On dock, charged"

    async def on_startup(self) -> None:
        """Nothing to start: the sim publishes from its own flight task."""

    @property
    def flying(self) -> bool:
        return self.task is not None and not self.task.done()

    def _say(self, mode: str, message: str, severity: int = vitals.INFO) -> None:
        if mode != self.mode:
            self.msglog.add(f"Mode {mode}", vitals.NOTICE, "planner")
        self.mode, self.message = mode, message
        self.msglog.add(message, severity, "planner")

    def telemetry(self) -> dict[str, Any]:
        lon, lat = self.frame.to_lonlat(self.x, self.y)
        pct = max(self.battery, 0.0)
        airborne = self.mode not in ("DOCKED",)
        current = HOVER_A * (1.1 if self.speed > 0 else 1.0) if airborne and self.alt > 0 else IDLE_A
        voltage = V_EMPTY + (V_FULL - V_EMPTY) * pct / 100 - 0.015 * current
        used = round(CAPACITY_MAH * (100 - pct) / 100)
        return {
            "t": time.time(),
            "lat": lat,
            "lon": lon,
            "alt_m": round(self.alt, 1),
            "heading_deg": round(self.heading % 360),
            "roll_deg": 0.0,
            # A multirotor leans into its direction of travel; about 5 degrees at cruise.
            "pitch_deg": -5.0 if self.speed > 0 else 0.0,
            "groundspeed_ms": round(self.speed, 1),
            "battery_pct": round(pct, 1),
            "voltage_v": round(voltage, 2),
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
            "source": "sim",
            # Dashboard panels. Placeholders where the sim has no model.
            "climb_ms": round(self.climb, 1),
            "airspeed_ms": round(self.speed, 1),
            "throttle_pct": 45 if airborne and self.alt > 0 else 0,
            "alt_msl_m": None,
            "system_status": "ACTIVE" if airborne else "STANDBY",
            "armed": airborne,
            "dock_distance_m": round(math.hypot(self.x, self.y), 1),
            "dock_bearing_deg": round(math.degrees(math.atan2(self.x, self.y))) % 360,
            "battery": {
                "voltage_v": round(voltage, 2),
                "current_a": round(current, 1),
                "remaining_pct": round(pct, 1),
                "consumed_mah": used,
                "capacity_mah": CAPACITY_MAH,
                "time_remaining_s": round(ENDURANCE_S * pct / 100) if airborne else None,
            },
            "gps": {"fix": "3D", "fix_type": 3, "satellites": 14, "hdop": 0.7, "vdop": 1.1, "h_acc_m": None},
            "ekf": vitals.ekf_status(0x3FF & ~vitals.mavlink.EKF_CONST_POS_MODE & ~vitals.mavlink.EKF_UNINITIALIZED, {
                "velocity": 0.05, "pos_horiz": 0.04, "pos_vert": 0.06, "compass": 0.03, "terrain": 0.0,
            }),  # fmt: skip
            "vibration": {"x": 8.0, "y": 8.0, "z": 12.0, "clipping": [0, 0, 0], "level": "ok"} if airborne else {"x": 0.1, "y": 0.1, "z": 0.1, "clipping": [0, 0, 0], "level": "ok"},
            "rangefinder": {
                "distance_m": round(min(self.alt, vitals.RANGEFINDER_MAX_M + 1), 2),
                "min_m": vitals.RANGEFINDER_MIN_M,
                "max_m": vitals.RANGEFINDER_MAX_M,
                "in_range": vitals.RANGEFINDER_MIN_M <= self.alt < vitals.RANGEFINDER_MAX_M,
                "age_s": 0.0,
            },
            "sensors": [{"name": n, "enabled": True, "healthy": True} for n in ("Gyro", "Accel", "Compass", "Baro", "GPS", "AHRS", "Rangefinder", "Battery")],
            "prearm": {"ok": True, "reasons": []},
            "link": {"connected": True, "endpoint": "built-in sim", "heartbeat_age_s": 0.0, "packet_loss_pct": 0.0, "vehicle_drop_pct": 0.0, "rc_rssi_pct": None},
            "nav": {"wp_dist_m": round(self.wp_dist), "xtrack_m": 0.0, "mission_seq": None},
            "messages": self.msglog.tail(12),
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
            self.msglog.add("Armed", vitals.NOTICE, "planner")
            self._say("TAKEOFF", "Taking off from dock")
            while self.alt < ALT_M:
                self.alt = min(ALT_M, self.alt + CLIMB_MS * dt)
                self.speed, self.climb = 0.0, CLIMB_MS
                await self._step(dt)
            self.climb = 0.0
            self._say("AUTO", "Surveying")
            path = list(waypoints)
            k = 1
            while k < len(path):
                if self.abort_requested and self.mode != "RTL":
                    self._say("RTL", "Abort: returning to dock", vitals.WARNING)
                    path = [(self.x, self.y), (0.0, 0.0)]
                    k = 1
                    continue
                tx, ty = path[k]
                d = math.dist((self.x, self.y), (tx, ty))
                self.wp_dist = d
                step = CRUISE_MS * dt
                if d > 0.01:
                    self.heading = math.degrees(math.atan2(tx - self.x, ty - self.y))
                if d <= step:
                    self.flown_m += d
                    self.x, self.y = tx, ty
                    k += 1
                    if k == len(path) - 1 and self.mode == "AUTO":
                        self._say("RTL", "Survey done, returning to dock")
                    if d < step * 0.5:
                        continue  # don't burn a tick on a tiny leg
                else:
                    self.x += (tx - self.x) / d * step
                    self.y += (ty - self.y) / d * step
                    self.flown_m += step
                self.speed = CRUISE_MS
                await self._step(dt)
            self._say("LAND", "Landing on dock")
            self.speed = 0.0
            while self.alt > 0:
                self.alt = max(0.0, self.alt - CLIMB_MS * dt)
                self.climb = -CLIMB_MS
                await self._step(dt)
            self.climb = 0.0
            self.msglog.add("Disarmed", vitals.NOTICE, "planner")
            self._say("DOCKED", "Aborted and docked" if self.abort_requested else "Mission complete, charging on dock")
            await self.publish()
        except asyncio.CancelledError:
            self.mode, self.message = "DOCKED", "Simulation stopped"
            raise
