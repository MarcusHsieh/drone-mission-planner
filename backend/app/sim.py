"""Simulated drone for M1 (ArduPilot SITL replaces this later).

Flies the previewed route: take off at the dock, fly every group's passes in
order, return, land. Publishes telemetry shaped like what we'll read from
MAVLink (position, altitude, heading, speed, battery, mode, GPS) at 5 Hz.
The battery drains by the planner's energy model; the UI labels it simulated.
The dashboard's health panels (EKF, vibration, sensors, link) get steady
"all good" values so the layout can be tried without SITL; only SITL's are real.
"""

from __future__ import annotations

import asyncio
import math
import time
from typing import Any

from . import config, planner, vitals
from .energy import EnergyModel
from .geo import LocalFrame
from .vehicle import LegMission, LegProgress, LegWaypoint

TICK_S = 0.2  # 5 Hz
ALT_M = planner.ALTITUDE_M
IDLE_A = 0.4
# The battery follows the planner's energy model (a 4S pack, placeholder
# currents per flight phase, see energy.py) without its safety margin, so the
# sim draws a little less than the plan predicts, as a real flight should.
ENERGY = EnergyModel.from_env()


class DroneSim:
    def __init__(self, frame: LocalFrame, energy: EnergyModel | None = None) -> None:
        self.frame = frame
        self.energy = energy or ENERGY
        self.subscribers: set[asyncio.Queue] = set()
        self.task: asyncio.Task | None = None
        self._idle: asyncio.Task | None = None
        self.abort_requested = False
        self.msglog = vitals.MessageLog()
        self.msglog.add("Built-in simulator ready (health values are placeholders)", vitals.INFO, "planner")
        self._reset()

        self.connected = True
        self.consumed_mah = 0.0  # since the last recharge; survives between flights like a real pack
        self.progress = LegProgress(finished=True)
        self._leg_mah0 = 0.0

    def _reset(self) -> None:
        self.x = self.y = 0.0
        self.alt = 0.0
        self.heading = 0.0
        self.speed = 0.0
        self.climb = 0.0
        self.wp_dist = 0.0
        self.current = IDLE_A
        self.mode = "DOCKED"
        self.flown_m = 0.0
        self.route_m = 0.0
        self.sim_elapsed = 0.0
        self.message = "On dock, charged"

    @property
    def battery(self) -> float:
        return 100 * (1 - self.consumed_mah / self.energy.pack_mah)

    async def on_startup(self) -> None:
        """Keep publishing while docked (the flight task publishes in flight),
        so the dashboard can tell a quiet sim from a dead connection."""
        if getattr(self, "_idle", None) is None or self._idle.done():
            self._idle = asyncio.create_task(self._idle_loop())

    async def _idle_loop(self) -> None:
        while True:
            if not self.flying:
                await self.publish()
            await asyncio.sleep(0.5)

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
        current = self.current if airborne else IDLE_A
        voltage = self.energy.cells * (3.5 + 0.7 * pct / 100) - 0.015 * current
        left_mah = max(self.energy.pack_mah - self.consumed_mah, 0.0)
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
            "connected": True,
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
                "consumed_mah": round(self.consumed_mah),
                "capacity_mah": round(self.energy.pack_mah),
                "time_remaining_s": round(left_mah / current * 3.6) if airborne and current > 1 else None,
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

    # -- flying ------------------------------------------------------------------
    def start(self, waypoints: list[tuple[float, float]]) -> None:
        """Fly a survey route (dock -> points -> dock) at survey altitude."""
        inner = waypoints[1:-1] if len(waypoints) > 2 else waypoints[1:]
        leg = LegMission(0, [LegWaypoint(x, y, ALT_M) for x, y in inner], ALT_M, self.energy.cruise_ms)
        self._begin(leg, sum(math.dist(a, b) for a, b in zip(waypoints, waypoints[1:])), "Surveying")

    def start_leg(self, leg: LegMission) -> None:
        pts = [(0.0, 0.0)] + [(w.x, w.y) for w in leg.waypoints] + [(0.0, 0.0)]
        self._begin(leg, sum(math.dist(a, b) for a, b in zip(pts, pts[1:])), f"Leg {leg.index}")

    def _begin(self, leg: LegMission, route_m: float, label: str) -> None:
        if self.flying:
            raise RuntimeError("Already flying")
        self._reset()
        self.abort_requested = False
        self.route_m = route_m
        self.progress = LegProgress()
        self._leg_mah0 = self.consumed_mah
        self.task = asyncio.create_task(self._fly(leg, label))

    def leg_progress(self) -> LegProgress:
        self.progress.flight_s = self.sim_elapsed
        self.progress.consumed_mah = self.consumed_mah - self._leg_mah0
        return self.progress

    def abort(self) -> None:
        self.abort_requested = True

    def return_to_dock(self) -> None:
        self.abort_requested = True

    def recharge(self) -> None:
        self.consumed_mah = 0.0
        self.msglog.add("Battery recharged on the dock (simulated)", vitals.INFO, "planner")
        if not self.flying:
            self.message = "On dock, charged"

    async def _step(self, dt_sim: float, current: float) -> None:
        self.sim_elapsed += dt_sim
        self.current = current
        self.consumed_mah += current * dt_sim / 3.6
        await self.publish()
        await asyncio.sleep(TICK_S)

    async def _goto(self, tx: float, ty: float, talt: float, speed: float) -> bool:
        """Fly to (tx, ty) at the current altitude, then climb or descend to talt.
        Returns False if an abort came in on the way."""
        while True:
            if self.abort_requested and self.mode in ("TAKEOFF", "AUTO"):
                return False
            dt = TICK_S * config.SIM_SPEEDUP
            d = math.dist((self.x, self.y), (tx, ty))
            self.wp_dist = d
            if d > 0.01:
                self.heading = math.degrees(math.atan2(tx - self.x, ty - self.y))
                step = speed * dt
                self.climb = 0.0
                if d <= step:
                    self.flown_m += d
                    self.x, self.y = tx, ty
                    dt = d / speed  # only the time it took
                else:
                    self.x += (tx - self.x) / d * step
                    self.y += (ty - self.y) / d * step
                    self.flown_m += step
                self.speed = speed
                await self._step(dt, self.energy.cruise_a)
                continue
            dz = talt - self.alt
            if abs(dz) > 0.01:
                rate = self.energy.climb_ms if dz > 0 else self.energy.descent_ms
                self.speed = 0.0
                self.climb = rate if dz > 0 else -rate
                if abs(dz) <= rate * dt:
                    self.alt, dt = talt, abs(dz) / rate
                else:
                    self.alt += math.copysign(rate * dt, dz)
                await self._step(dt, self.energy.climb_a if dz > 0 else self.energy.descent_a)
                continue
            self.climb = 0.0
            return True

    async def _hold(self, seconds: float) -> bool:
        left = seconds
        self.speed = 0.0
        while left > 0:
            if self.abort_requested:
                return False
            dt = min(TICK_S * config.SIM_SPEEDUP, left)
            left -= dt
            await self._step(dt, self.energy.hover_a)
        return True

    async def _fly(self, leg: LegMission, label: str) -> None:
        p = self.progress
        try:
            self.msglog.add("Armed", vitals.NOTICE, "planner")
            self._say("TAKEOFF", "Taking off from dock")
            await self._goto(0.0, 0.0, leg.cruise_alt_m, leg.speed_ms)
            self._say("AUTO", label)
            survey = leg.index == 0
            for k, w in enumerate(leg.waypoints):
                if w.role == "approach":
                    p.active_task = w.task_id
                    self.message = f"{label}: flying to {w.task_id}"
                elif w.role == "service":
                    self.message = f"{label}: working on {w.task_id}"
                if not await self._goto(w.x, w.y, w.alt_m, leg.speed_ms):
                    break
                if w.hold_s and not await self._hold(w.hold_s):
                    break
                if w.role == "service" and w.task_id:
                    p.done_tasks.append(w.task_id)
                    p.active_task = None
                    self.msglog.add(f"{w.task_id} done", vitals.INFO, "planner")
                if survey and k == len(leg.waypoints) - 1:
                    self._say("RTL", "Survey done, returning to dock")
            if self.abort_requested:
                p.returning = True
                p.active_task = None
                self._say("RTL", "Returning to dock early", vitals.WARNING)
                await self._goto(self.x, self.y, max(self.alt, leg.cruise_alt_m), leg.speed_ms)
            elif not survey:
                self._say("RTL", f"{label} done, returning to dock")
            await self._goto(0.0, 0.0, max(self.alt, leg.cruise_alt_m), leg.speed_ms)
            self._say("LAND", "Landing on dock")
            self.speed = 0.0
            await self._goto(0.0, 0.0, 0.0, leg.speed_ms)
            self.msglog.add("Disarmed", vitals.NOTICE, "planner")
            self._say("DOCKED", "Returned early and docked" if self.abort_requested else "Landed on dock")
            await self.publish()
        except asyncio.CancelledError:
            self.mode, self.message = "DOCKED", "Simulation stopped"
            raise
        finally:
            p.finished = True
