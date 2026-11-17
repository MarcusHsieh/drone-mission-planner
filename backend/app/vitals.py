"""Vehicle health and status that a ground station shows during a flight.

Shared by the built-in sim (`sim.DroneSim`) and ArduPilot SITL
(`sitl_drone.SitlDrone`) so both publish the same telemetry shape for the
dashboard panels: the message log (MAVLink STATUSTEXT plus the planner's own
events), sensor health from SYS_STATUS, and the thresholds the panels color by.
"""

from __future__ import annotations

import os
import threading
import time
from collections import deque
from typing import Any

os.environ.setdefault("MAVLINK20", "1")

from pymavlink import mavutil  # noqa: E402

mavlink = mavutil.mavlink

# MAVLink MAV_SEVERITY, 0 is the worst.
SEVERITY = ["EMERGENCY", "ALERT", "CRITICAL", "ERROR", "WARNING", "NOTICE", "INFO", "DEBUG"]
INFO = 6
NOTICE = 5
WARNING = 4
ERROR = 3

# SYS_STATUS sensor bits worth a light on the dashboard, in display order.
SENSORS: list[tuple[str, int]] = [
    ("Gyro", mavlink.MAV_SYS_STATUS_SENSOR_3D_GYRO),
    ("Accel", mavlink.MAV_SYS_STATUS_SENSOR_3D_ACCEL),
    ("Compass", mavlink.MAV_SYS_STATUS_SENSOR_3D_MAG),
    ("Baro", mavlink.MAV_SYS_STATUS_SENSOR_ABSOLUTE_PRESSURE),
    ("GPS", mavlink.MAV_SYS_STATUS_SENSOR_GPS),
    ("AHRS", mavlink.MAV_SYS_STATUS_AHRS),
    ("Rangefinder", mavlink.MAV_SYS_STATUS_SENSOR_LASER_POSITION),
    ("RC receiver", mavlink.MAV_SYS_STATUS_SENSOR_RC_RECEIVER),
    ("Battery", mavlink.MAV_SYS_STATUS_SENSOR_BATTERY),
    ("Motors", mavlink.MAV_SYS_STATUS_SENSOR_MOTOR_OUTPUTS),
    ("Logging", mavlink.MAV_SYS_STATUS_LOGGING),
    ("Geofence", mavlink.MAV_SYS_STATUS_GEOFENCE),
]
PREARM_BIT = mavlink.MAV_SYS_STATUS_PREARM_CHECK

# EKF_STATUS_REPORT flags that must be set for a GPS mission.
EKF_REQUIRED = {
    "attitude": mavlink.EKF_ATTITUDE,
    "velocity_horiz": mavlink.EKF_VELOCITY_HORIZ,
    "velocity_vert": mavlink.EKF_VELOCITY_VERT,
    "pos_horiz_abs": mavlink.EKF_POS_HORIZ_ABS,
    "pos_vert_abs": mavlink.EKF_POS_VERT_ABS,
}

MAV_STATE = {
    mavlink.MAV_STATE_UNINIT: "UNINIT",
    mavlink.MAV_STATE_BOOT: "BOOT",
    mavlink.MAV_STATE_CALIBRATING: "CALIBRATING",
    mavlink.MAV_STATE_STANDBY: "STANDBY",
    mavlink.MAV_STATE_ACTIVE: "ACTIVE",
    mavlink.MAV_STATE_CRITICAL: "CRITICAL",
    mavlink.MAV_STATE_EMERGENCY: "EMERGENCY",
    mavlink.MAV_STATE_POWEROFF: "POWEROFF",
    mavlink.MAV_STATE_FLIGHT_TERMINATION: "TERMINATION",
}

# Benewake TFmini-S, the airframe's downward rangefinder: 0.1 to 12 m on a
# bright target indoors, less outdoors on grass in sunlight.
RANGEFINDER_MIN_M = 0.1
RANGEFINDER_MAX_M = 12.0


class MessageLog:
    """The vehicle's STATUSTEXT messages and the planner's own events, newest last.

    Each entry gets an increasing id so a client that only sees the last few
    (the WebSocket sends a tail) can tell which ones it already has.
    """

    def __init__(self, maxlen: int = 300) -> None:
        self._items: deque[dict[str, Any]] = deque(maxlen=maxlen)
        self._next = 1
        self._lock = threading.Lock()

    def add(self, text: str, severity: int = INFO, source: str = "vehicle") -> None:
        text = text.strip()
        if not text:
            return
        severity = min(max(int(severity), 0), 7)
        with self._lock:
            self._items.append(
                {"id": self._next, "t": time.time(), "severity": severity, "level": SEVERITY[severity], "source": source, "text": text}
            )
            self._next += 1

    def tail(self, n: int) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._items)[-n:]

    def all(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._items)

    @property
    def last_id(self) -> int:
        return self._next - 1


def sensor_health(present: int, enabled: int, health: int) -> list[dict[str, Any]]:
    """SYS_STATUS bitmasks to a list of sensors with a health light each."""
    out = []
    for name, bit in SENSORS:
        if present & bit:
            out.append({"name": name, "enabled": bool(enabled & bit), "healthy": bool(health & bit)})
    return out


def prearm_status(present: int, enabled: int, health: int, reasons: list[str]) -> dict[str, Any]:
    """ArduPilot reports 'pre-arm checks pass' as the PREARM_CHECK health bit."""
    if not present & PREARM_BIT:
        return {"ok": None, "reasons": reasons}
    return {"ok": bool(health & PREARM_BIT) or not enabled & PREARM_BIT, "reasons": reasons}


def ekf_status(flags: int, variances: dict[str, float]) -> dict[str, Any]:
    missing = [name for name, bit in EKF_REQUIRED.items() if not flags & bit]
    worst = max(variances.values()) if variances else 0.0
    return {
        "flags_ok": not missing,
        "missing": missing,
        "const_pos_mode": bool(flags & mavlink.EKF_CONST_POS_MODE),
        "gps_glitching": bool(flags & mavlink.EKF_GPS_GLITCHING),
        "variances": {k: round(v, 3) for k, v in variances.items()},
        # Mission Planner's EKF window: under 0.5 is good, 0.5 to 0.8 is a warning, over 0.8 is bad.
        "level": "bad" if worst >= 0.8 or missing else "warn" if worst >= 0.5 else "ok",
    }


def vibration_level(vx: float, vy: float, vz: float) -> str:
    """ArduPilot's guidance: under 30 m/s/s is fine, over 60 often upsets the EKF."""
    worst = max(vx, vy, vz)
    return "bad" if worst > 60 else "warn" if worst > 30 else "ok"
