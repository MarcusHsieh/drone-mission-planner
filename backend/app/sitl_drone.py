"""Fly the planner's route on ArduPilot (SITL now, the real autopilot later).

Drop-in replacement for `sim.DroneSim` when FLIGHT_BACKEND=sitl: same
`start`, `abort`, `telemetry`, `subscribers` and `flying`, so the API and the
frontend don't change. Instead of moving a point along the route itself, it

1. uploads the route as a Copter mission (`mavlink_mission.build_mission`),
2. switches to GUIDED and arms, then AUTO and MISSION_START,
3. reads the vehicle's own telemetry until it lands and disarms.

Abort switches to RTL, which flies back to where it armed (the dock) and lands.

Threads: pymavlink is blocking, so one reader thread owns the socket. It keeps
the latest telemetry and forwards every message to an inbox that the flight
thread reads while it uploads and arms. An asyncio task publishes telemetry to
the WebSocket subscribers at 5 Hz, like the simple sim does.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import queue
import threading
import time
from typing import Any

# MAVLink 2 (ArduPilot's default); must be set before pymavlink is imported.
os.environ.setdefault("MAVLINK20", "1")

from pymavlink import mavutil  # noqa: E402

from . import mavlink_mission as mm
from .geo import LocalFrame

log = logging.getLogger(__name__)
mavlink = mavutil.mavlink

TICK_S = 0.2
GPS_FIX = {0: "No GPS", 1: "No fix", 2: "2D", 3: "3D", 4: "DGPS", 5: "RTK float", 6: "RTK fixed"}
READY_TIMEOUT_S = 120  # SITL needs ~20-40 s after boot before the EKF lets it arm


class FlightError(RuntimeError):
    pass


class SitlDrone:
    def __init__(self, frame: LocalFrame, connection: str) -> None:
        self.frame = frame
        self.connection = connection
        self.subscribers: set[asyncio.Queue] = set()
        self.master = None
        self.connected = False
        self._inbox: queue.Queue = queue.Queue(maxsize=500)
        self._listening = threading.Event()  # set while the flight thread reads the inbox
        self._flight: threading.Thread | None = None
        self._pump: asyncio.Task | None = None
        self._lock = threading.Lock()
        self.abort_requested = False
        self.statustext: list[str] = []
        self.v: dict[str, Any] = {
            "lat": frame.lat0, "lon": frame.lon0, "alt": 0.0, "heading": 0, "speed": 0.0,
            "battery": 100.0, "voltage": 0.0, "fix": 0, "sats": 0, "ap_mode": "UNKNOWN",
            "armed": False, "mission_seq": 0, "boot_ms": 0,
        }  # fmt: skip
        self._reset_flight()
        threading.Thread(target=self._reader, name="mavlink-reader", daemon=True).start()

    # -- flight bookkeeping ---------------------------------------------------
    def _reset_flight(self) -> None:
        self.route_m = 0.0
        self.flown_m = 0.0
        self.items: list[mm.MissionItem] = []
        self.reached: list[int] = []  # survey waypoint seqs, in the order the autopilot reported them
        self._waypoint_seqs: set[int] = set()
        self.track: list[tuple[float, float]] = []  # (x, y) in the dock frame while armed
        self.arm_boot_ms: int | None = None
        self.arm_wall: float | None = None
        self.phase = "IDLE"  # IDLE, PREPARING, FLYING, DONE, FAILED
        self.message = "Waiting for ArduPilot SITL" if not self.connected else "On dock, ready"

    @property
    def flying(self) -> bool:
        return self.phase in ("PREPARING", "FLYING")

    # -- telemetry -------------------------------------------------------------
    def _display_mode(self) -> str:
        v = self.v
        if not v["armed"]:
            return "PREFLIGHT" if self.phase == "PREPARING" else "DOCKED"
        m = v["ap_mode"]
        if m == "AUTO" and self.items:
            seq = v["mission_seq"]
            if seq <= 2:
                return "TAKEOFF"
            if seq == self.items[-1].seq:
                return "LAND"
        if m == "GUIDED":
            return "TAKEOFF"
        return m

    def telemetry(self) -> dict[str, Any]:
        v = self.v
        elapsed = 0.0
        speedup = 1.0
        if self.arm_boot_ms is not None:
            elapsed = max(0.0, (v["boot_ms"] - self.arm_boot_ms) / 1000)
            wall = time.monotonic() - (self.arm_wall or time.monotonic())
            if wall > 5:
                speedup = round(elapsed / wall, 1)
        total_wp = len(mm.nav_seqs(self.items)) if self.items else 0
        return {
            "t": time.time(),
            "lat": v["lat"],
            "lon": v["lon"],
            "alt_m": round(v["alt"], 1),
            "heading_deg": round(v["heading"]) % 360,
            "groundspeed_ms": round(v["speed"], 1),
            "battery_pct": round(v["battery"], 1),
            "voltage_v": round(v["voltage"], 2),
            "mode": self._display_mode(),
            "autopilot_mode": v["ap_mode"],
            "armed": v["armed"],
            "gps_fix": GPS_FIX.get(v["fix"], str(v["fix"])),
            "satellites": v["sats"],
            "distance_flown_m": round(self.flown_m),
            "route_m": round(self.route_m),
            "progress_pct": round(100 * len(self.reached) / total_wp) if total_wp else 0,
            "waypoints_reached": len(self.reached),
            "waypoints_total": total_wp,
            "elapsed_s": round(elapsed),
            "speedup": speedup if speedup >= 1.5 else 1,
            "message": self.message,
            "simulated": True,
            "source": "sitl",
            "connected": self.connected,
        }

    async def on_startup(self) -> None:
        if self._pump is None or self._pump.done():
            self._pump = asyncio.create_task(self._publish_loop())

    async def _publish_loop(self) -> None:
        while True:
            msg = self.telemetry()
            for q in list(self.subscribers):
                if q.full():
                    try:
                        q.get_nowait()
                    except asyncio.QueueEmpty:
                        pass
                q.put_nowait(msg)
            await asyncio.sleep(TICK_S)

    # -- reader thread -----------------------------------------------------------
    def _reader(self) -> None:
        while True:
            try:
                log.info("Connecting to SITL at %s", self.connection)
                m = mavutil.mavlink_connection(self.connection, source_system=255, source_component=190)
                hb = m.wait_heartbeat(timeout=10)
                if hb is None:
                    raise ConnectionError("no heartbeat")
                self.master = m
                # SITL's TCP ports send nothing until asked. 5 Hz of everything is plenty.
                m.mav.request_data_stream_send(m.target_system, m.target_component, mavlink.MAV_DATA_STREAM_ALL, 5, 1)
                self.connected = True
                if self.phase == "IDLE":
                    self.message = "On dock, ready"
                log.info("Connected to SITL (system %s)", m.target_system)
                last_rx = time.monotonic()
                while True:
                    msg = m.recv_match(blocking=True, timeout=1.0)
                    if msg is None:
                        if time.monotonic() - last_rx > 5:
                            raise ConnectionError("telemetry stopped")
                        continue
                    last_rx = time.monotonic()
                    self._handle(m, msg)
            except Exception as e:  # noqa: BLE001 - keep retrying whatever went wrong
                if self.connected:
                    log.warning("Lost SITL connection: %s", e)
                self.connected = False
                self.master = None
                if not self.flying:
                    self.message = f"Waiting for ArduPilot SITL at {self.connection}"
                time.sleep(2)

    def _handle(self, m, msg) -> None:
        t = msg.get_type()
        v = self.v
        if t == "HEARTBEAT" and msg.get_srcSystem() == m.target_system and msg.get_srcComponent() == 1:
            v["ap_mode"] = mavutil.mode_string_v10(msg)
            armed = bool(msg.base_mode & mavlink.MAV_MODE_FLAG_SAFETY_ARMED)
            if armed and not v["armed"]:
                self.arm_boot_ms = v["boot_ms"]
                self.arm_wall = time.monotonic()
                self.track = []
            v["armed"] = armed
        elif t == "GLOBAL_POSITION_INT":
            v["lat"], v["lon"] = msg.lat / 1e7, msg.lon / 1e7
            v["alt"] = msg.relative_alt / 1000
            v["boot_ms"] = msg.time_boot_ms
            if msg.hdg != 65535:
                v["heading"] = msg.hdg / 100
            if v["armed"]:
                p = self.frame.to_xy(v["lon"], v["lat"])
                if self.track:
                    self.flown_m += math.dist(p, self.track[-1])
                self.track.append(p)
        elif t == "VFR_HUD":
            v["speed"] = msg.groundspeed
        elif t == "SYS_STATUS":
            if msg.battery_remaining >= 0:
                v["battery"] = msg.battery_remaining
            v["voltage"] = msg.voltage_battery / 1000
        elif t == "GPS_RAW_INT":
            v["fix"], v["sats"] = msg.fix_type, msg.satellites_visible
        elif t == "MISSION_CURRENT":
            v["mission_seq"] = msg.seq
        elif t == "MISSION_ITEM_REACHED":
            if self.flying and msg.seq not in self.reached and msg.seq in self._waypoint_seqs:
                self.reached.append(msg.seq)
        elif t == "STATUSTEXT":
            text = msg.text.strip()
            self.statustext = (self.statustext + [text])[-20:]
            log.info("SITL: %s", text)
        if self._listening.is_set():
            try:
                self._inbox.put_nowait(msg)
            except queue.Full:
                pass

    # -- commands (flight thread) --------------------------------------------------
    def _recv(self, types: list[str], timeout: float):
        deadline = time.monotonic() + timeout
        while (left := deadline - time.monotonic()) > 0:
            try:
                msg = self._inbox.get(timeout=left)
            except queue.Empty:
                return None
            if msg.get_type() in types:
                return msg
        return None

    def _command(self, cmd: int, *params: float, timeout: float = 3.0) -> int | None:
        m = self.master
        if m is None:
            raise FlightError("Lost the SITL connection")
        p = list(params) + [0.0] * (7 - len(params))
        m.mav.command_long_send(m.target_system, m.target_component, cmd, 0, *p)
        deadline = time.monotonic() + timeout
        while (left := deadline - time.monotonic()) > 0:
            ack = self._recv(["COMMAND_ACK"], left)
            if ack is not None and ack.command == cmd:
                return ack.result
        return None

    def _set_mode(self, name: str) -> None:
        m = self.master
        if m is None:
            raise FlightError("Lost the SITL connection")
        mode_id = m.mode_mapping()[name]
        for _ in range(5):
            res = self._command(mavlink.MAV_CMD_DO_SET_MODE, mavlink.MAV_MODE_FLAG_CUSTOM_MODE_ENABLED, mode_id)
            if res == mavlink.MAV_RESULT_ACCEPTED:
                return
            time.sleep(0.5)
        raise FlightError(f"Autopilot refused mode {name}")

    def _arm(self) -> None:
        deadline = time.monotonic() + READY_TIMEOUT_S
        while time.monotonic() < deadline:
            if self.abort_requested:
                raise FlightError("Aborted before takeoff")
            res = self._command(mavlink.MAV_CMD_COMPONENT_ARM_DISARM, 1)
            if res == mavlink.MAV_RESULT_ACCEPTED:
                return
            prearm = [s for s in self.statustext if s.startswith(("PreArm", "Arm"))]
            self.message = f"Waiting to arm: {prearm[-1]}" if prearm else "Waiting for the autopilot to be ready to arm"
            time.sleep(2)
        raise FlightError(f"Could not arm within {READY_TIMEOUT_S} s. {self.message}")

    def start(self, waypoints: list[tuple[float, float]]) -> None:
        with self._lock:
            if self.flying:
                raise RuntimeError("Already flying")
            if not self.connected:
                raise RuntimeError(f"Not connected to ArduPilot SITL at {self.connection}. Is it running?")
            if self.v["armed"]:
                raise RuntimeError("The vehicle is already armed")
            self._reset_flight()
            self.abort_requested = False
            self.items = mm.build_mission(waypoints, self.frame)
            self._waypoint_seqs = set(mm.nav_seqs(self.items))
            self.route_m = sum(math.dist(a, b) for a, b in zip(waypoints, waypoints[1:]))
            self.phase = "PREPARING"
            self._flight = threading.Thread(target=self._fly, name="sitl-flight", daemon=True)
            self._flight.start()

    def _fly(self) -> None:
        self._inbox = queue.Queue(maxsize=500)
        self._listening.set()
        try:
            self.message = f"Uploading mission ({len(self.items)} items)"
            mm.upload_mission(self.master, self.items, self._recv)
            self.message = "Mission uploaded, arming"
            self._set_mode("GUIDED")
            self._arm()
            self._set_mode("AUTO")
            # Copter waits for throttle before an AUTO takeoff from the ground; MISSION_START skips that.
            res = self._command(mavlink.MAV_CMD_MISSION_START, 0, 0)
            if res != mavlink.MAV_RESULT_ACCEPTED:
                raise FlightError(f"Autopilot refused MISSION_START (result {res})")
            self.phase = "FLYING"
            self._listening.clear()
            self.message = "Flying the mission"
            if self.abort_requested:
                self.abort()
            # Wait for the vehicle to land and disarm (Copter disarms itself after NAV_LAND or RTL).
            while self.v["armed"]:
                if not self.connected:
                    raise FlightError("Lost the SITL connection in flight")
                if not self.abort_requested:
                    total = len(self._waypoint_seqs)
                    if self._display_mode() == "LAND":
                        self.message = f"Landing on the dock ({len(self.reached)} of {total} waypoints reached)"
                    elif self._display_mode() == "TAKEOFF":
                        self.message = "Taking off from the dock"
                    else:
                        self.message = f"Surveying, {len(self.reached)} of {total} waypoints reached"
                time.sleep(TICK_S)
            self.phase = "DONE"
            self.message = "Aborted, landed at the dock" if self.abort_requested else "Mission complete, landed at the dock"
        except Exception as e:  # noqa: BLE001
            log.exception("SITL flight failed")
            self.phase = "FAILED"
            self.message = f"Flight failed: {e}"
            if self.v["armed"] and self.master is not None:
                try:
                    self.master.set_mode("RTL")
                except Exception:  # noqa: BLE001
                    pass
        finally:
            self._listening.clear()

    def abort(self) -> None:
        self.abort_requested = True
        if self.phase == "FLYING" and self.master is not None:
            self.message = "Abort: RTL, returning to the dock"
            self.master.set_mode("RTL")  # fire and forget; the heartbeat shows whether it took

    def wait_done(self, timeout_s: float) -> bool:
        """Block until the current flight ends (for scripts and tests)."""
        t = self._flight
        if t is None:
            return True
        t.join(timeout_s)
        return not t.is_alive()
