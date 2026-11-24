"""Fly the planner's route on ArduPilot (SITL now, the real autopilot later).

Drop-in replacement for `sim.DroneSim` when FLIGHT_BACKEND=sitl: same
`start`, `abort`, `telemetry`, `subscribers` and `flying`, so the API and the
frontend don't change. Instead of moving a point along the route itself, it

1. uploads the route as a Copter mission (`mavlink_mission.build_mission`),
2. switches to GUIDED and arms, then AUTO and MISSION_START,
3. reads the vehicle's own telemetry until it lands and disarms.

Abort switches to RTL, which flies back to where it armed (the dock) and lands.

It is also the task queue's vehicle link (see vehicle.py): `start_leg` flies
one leg as a mission with a hold at each task, `leg_progress` reads which
tasks are done from the mission sequence, and `recharge` resets the battery
monitor after a simulated charge. Nothing else here is SITL-specific: point
SITL_CONNECTION at a real Pixhawk and the same code flies it.

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
from . import vitals
from .geo import LocalFrame
from .vehicle import LegMission, LegProgress

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
        self._closed = threading.Event()
        self.abort_requested = False
        self.statustext: list[str] = []
        self.v: dict[str, Any] = {
            "lat": frame.lat0, "lon": frame.lon0, "alt": 0.0, "heading": 0, "speed": 0.0, "roll": 0.0, "pitch": 0.0,
            "battery": 100.0, "voltage": 0.0, "fix": 0, "sats": 0, "ap_mode": "UNKNOWN",
            "armed": False, "mission_seq": 0, "boot_ms": 0,
            # Everything below is for the dashboard panels.
            "climb": 0.0, "airspeed": 0.0, "throttle": 0, "alt_msl": None, "system_status": None,
            "current": None, "consumed_mah": None, "time_remaining_s": None, "capacity_mah": None,
            "hdop": None, "vdop": None, "h_acc": None,
            "sys_present": 0, "sys_enabled": 0, "sys_health": 0, "comm_drop_pct": None,
            "ekf_flags": None, "ekf_var": {}, "vibe": None, "rng": None, "rc_rssi": None,
            "wp_dist": None, "xtrack": None, "hb_wall": None,
        }  # fmt: skip
        self.msglog = vitals.MessageLog()
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
        self.leg: LegMission | None = None
        self.task_seqs: dict[str, dict[str, int]] = {}
        self._max_seq = 0  # highest MISSION_CURRENT seen in AUTO during this flight
        self.leg_start_mah: float | None = None
        self.flight_s: float | None = None  # arm to disarm, autopilot time
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
            "roll_deg": round(v["roll"], 1),
            "pitch_deg": round(v["pitch"], 1),
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
            **self._vitals(),
        }

    def _vitals(self) -> dict[str, Any]:
        """The dashboard's extra panels: HUD, battery, GPS, health, link, rangefinder, messages."""
        v = self.v
        now = time.monotonic()
        dock_x, dock_y = self.frame.to_xy(v["lon"], v["lat"])
        prearm_reasons = [s for s in self.statustext[-8:] if s.startswith("PreArm")]
        rng = v["rng"]
        rangefinder = None
        if rng is not None:
            dist, lo, hi, age_t = rng
            rangefinder = {
                "distance_m": round(dist, 2),
                "min_m": lo,
                "max_m": hi,
                # ArduPilot reports max+1 m (or max) when nothing is in range.
                "in_range": lo <= dist < hi,
                "age_s": round(now - age_t, 1),
            }
        ekf = None
        if v["ekf_flags"] is not None:
            ekf = vitals.ekf_status(v["ekf_flags"], v["ekf_var"])
        vibe = None
        if v["vibe"] is not None:
            x, y, z, c0, c1, c2 = v["vibe"]
            vibe = {"x": round(x, 1), "y": round(y, 1), "z": round(z, 1), "clipping": [c0, c1, c2], "level": vitals.vibration_level(x, y, z)}
        m = self.master
        loss = None
        if m is not None and getattr(m, "mav_count", 0) > 50:
            loss = round(m.packet_loss(), 1)
        return {
            "climb_ms": round(v["climb"], 1),
            "airspeed_ms": round(v["airspeed"], 1),
            "throttle_pct": v["throttle"],
            "alt_msl_m": None if v["alt_msl"] is None else round(v["alt_msl"], 1),
            "system_status": v["system_status"],
            "dock_distance_m": round(math.hypot(dock_x, dock_y), 1),
            "dock_bearing_deg": round(math.degrees(math.atan2(dock_x, dock_y))) % 360,
            "battery": {
                "voltage_v": round(v["voltage"], 2),
                "current_a": None if v["current"] is None else round(v["current"], 1),
                "remaining_pct": round(v["battery"], 1),
                "consumed_mah": v["consumed_mah"],
                "capacity_mah": v["capacity_mah"],
                "time_remaining_s": v["time_remaining_s"],
            },
            "gps": {
                "fix": GPS_FIX.get(v["fix"], str(v["fix"])),
                "fix_type": v["fix"],
                "satellites": v["sats"],
                "hdop": v["hdop"],
                "vdop": v["vdop"],
                "h_acc_m": v["h_acc"],
            },
            "ekf": ekf,
            "vibration": vibe,
            "rangefinder": rangefinder,
            "sensors": vitals.sensor_health(v["sys_present"], v["sys_enabled"], v["sys_health"]),
            "prearm": vitals.prearm_status(v["sys_present"], v["sys_enabled"], v["sys_health"], prearm_reasons),
            "link": {
                "connected": self.connected,
                "endpoint": self.connection,
                "heartbeat_age_s": None if v["hb_wall"] is None else round(now - v["hb_wall"], 1),
                "packet_loss_pct": loss,
                "vehicle_drop_pct": v["comm_drop_pct"],
                "rc_rssi_pct": v["rc_rssi"],
            },
            "nav": {"wp_dist_m": v["wp_dist"], "xtrack_m": v["xtrack"], "mission_seq": v["mission_seq"]},
            "messages": self.msglog.tail(12),
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
    def close(self) -> None:
        """Stop the reader and free the MAVLink port (SITL's serial ports take one client)."""
        self._closed.set()
        m = self.master
        if m is not None:
            try:
                m.close()
            except Exception:  # noqa: BLE001
                pass

    def _reader(self) -> None:
        while not self._closed.is_set():
            try:
                log.info("Connecting to SITL at %s", self.connection)
                m = mavutil.mavlink_connection(self.connection, source_system=255, source_component=190)
                hb = m.wait_heartbeat(timeout=10)
                if hb is None:
                    raise ConnectionError("no heartbeat")
                self.master = m
                # SITL's TCP ports send nothing until asked. 5 Hz of everything is plenty.
                m.mav.request_data_stream_send(m.target_system, m.target_component, mavlink.MAV_DATA_STREAM_ALL, 5, 1)
                # The pack size for the battery panel's mAh gauge.
                m.mav.param_request_read_send(m.target_system, m.target_component, b"BATT_CAPACITY", -1)
                self.connected = True
                self.msglog.add(f"Connected to the autopilot at {self.connection}", vitals.INFO, "planner")
                if self.phase == "IDLE":
                    self.message = "On dock, ready"
                log.info("Connected to SITL (system %s)", m.target_system)
                last_rx = time.monotonic()
                while not self._closed.is_set():
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
                    self.msglog.add(f"Lost the autopilot link: {e}", vitals.ERROR, "planner")
                self.connected = False
                self.master = None
                if not self.flying:
                    self.message = f"Waiting for ArduPilot SITL at {self.connection}"
                time.sleep(2)

    def _handle(self, m, msg) -> None:
        t = msg.get_type()
        v = self.v
        if t == "HEARTBEAT" and msg.get_srcSystem() == m.target_system and msg.get_srcComponent() == 1:
            mode = mavutil.mode_string_v10(msg)
            if mode != v["ap_mode"] and v["ap_mode"] != "UNKNOWN":
                self.msglog.add(f"Mode {mode}", vitals.NOTICE, "planner")
            v["ap_mode"] = mode
            v["hb_wall"] = time.monotonic()
            v["system_status"] = vitals.MAV_STATE.get(msg.system_status, str(msg.system_status))
            armed = bool(msg.base_mode & mavlink.MAV_MODE_FLAG_SAFETY_ARMED)
            if armed and not v["armed"]:
                # Battery reading at takeoff, so a leg's mAh is measured from here (after any recharge).
                self.leg_start_mah = v["consumed_mah"]
                self.arm_boot_ms = v["boot_ms"]
                self.arm_wall = time.monotonic()
                self.track = []
            if not armed and v["armed"] and self.arm_boot_ms is not None:
                self.flight_s = (v["boot_ms"] - self.arm_boot_ms) / 1000
            if armed != v["armed"]:
                self.msglog.add("Armed" if armed else "Disarmed", vitals.NOTICE, "planner")
            v["armed"] = armed
        elif t == "GLOBAL_POSITION_INT":
            v["lat"], v["lon"] = msg.lat / 1e7, msg.lon / 1e7
            v["alt"] = msg.relative_alt / 1000
            v["alt_msl"] = msg.alt / 1000
            v["boot_ms"] = msg.time_boot_ms
            if msg.hdg != 65535:
                v["heading"] = msg.hdg / 100
            if v["armed"]:
                p = self.frame.to_xy(v["lon"], v["lat"])
                if self.track:
                    self.flown_m += math.dist(p, self.track[-1])
                self.track.append(p)
        elif t == "ATTITUDE":
            v["roll"], v["pitch"] = math.degrees(msg.roll), math.degrees(msg.pitch)
        elif t == "VFR_HUD":
            v["speed"] = msg.groundspeed
            v["airspeed"], v["climb"], v["throttle"] = msg.airspeed, msg.climb, msg.throttle
        elif t == "SYS_STATUS":
            if msg.battery_remaining >= 0:
                v["battery"] = msg.battery_remaining
            v["voltage"] = msg.voltage_battery / 1000
            v["current"] = msg.current_battery / 100 if msg.current_battery >= 0 else None
            v["sys_present"] = msg.onboard_control_sensors_present
            v["sys_enabled"] = msg.onboard_control_sensors_enabled
            v["sys_health"] = msg.onboard_control_sensors_health
            v["comm_drop_pct"] = round(msg.drop_rate_comm / 100, 1)
        elif t == "BATTERY_STATUS" and msg.id == 0:
            if msg.current_consumed >= 0:
                v["consumed_mah"] = msg.current_consumed
            tr = getattr(msg, "time_remaining", 0)
            v["time_remaining_s"] = tr if tr else None
        elif t == "PARAM_VALUE" and msg.param_id == "BATT_CAPACITY":
            v["capacity_mah"] = round(msg.param_value)
        elif t == "GPS_RAW_INT":
            v["fix"], v["sats"] = msg.fix_type, msg.satellites_visible
            v["hdop"] = None if msg.eph == 65535 else round(msg.eph / 100, 2)
            v["vdop"] = None if msg.epv == 65535 else round(msg.epv / 100, 2)
            h_acc = getattr(msg, "h_acc", 0)
            v["h_acc"] = round(h_acc / 1000, 2) if h_acc else None
        elif t == "EKF_STATUS_REPORT":
            v["ekf_flags"] = msg.flags
            v["ekf_var"] = {
                "velocity": msg.velocity_variance,
                "pos_horiz": msg.pos_horiz_variance,
                "pos_vert": msg.pos_vert_variance,
                "compass": msg.compass_variance,
                "terrain": msg.terrain_alt_variance,
            }
        elif t == "VIBRATION":
            v["vibe"] = (msg.vibration_x, msg.vibration_y, msg.vibration_z, msg.clipping_0, msg.clipping_1, msg.clipping_2)
        elif t == "DISTANCE_SENSOR" and msg.orientation == mavlink.MAV_SENSOR_ROTATION_PITCH_270:
            v["rng"] = (msg.current_distance / 100, msg.min_distance / 100, msg.max_distance / 100, time.monotonic())
        elif t == "RC_CHANNELS":
            v["rc_rssi"] = None if msg.rssi == 255 else round(msg.rssi * 100 / 254)
        elif t == "NAV_CONTROLLER_OUTPUT":
            v["wp_dist"], v["xtrack"] = msg.wp_dist, round(msg.xtrack_error, 1)
        elif t == "MISSION_CURRENT":
            v["mission_seq"] = msg.seq
            if self.phase == "FLYING" and v["armed"] and v["ap_mode"] == "AUTO" and msg.seq < len(self.items):
                self._max_seq = max(self._max_seq, msg.seq)
        elif t == "MISSION_ITEM_REACHED":
            if self.flying and msg.seq not in self.reached and msg.seq in self._waypoint_seqs:
                self.reached.append(msg.seq)
        elif t == "STATUSTEXT":
            text = msg.text.strip()
            self.statustext = (self.statustext + [text])[-20:]
            self.msglog.add(text, msg.severity)
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
        self._begin(lambda: mm.build_mission(waypoints, self.frame), waypoints, None, {})

    def start_leg(self, leg: LegMission) -> None:
        items, where = mm.build_leg_mission(leg, self.frame)
        pts = [(0.0, 0.0)] + [(w.x, w.y) for w in leg.waypoints] + [(0.0, 0.0)]
        self._begin(lambda: items, pts, leg, where)

    def _begin(self, build, route_xy, leg: LegMission | None, where: dict) -> None:
        with self._lock:
            if self.flying:
                raise RuntimeError("Already flying")
            if not self.connected:
                raise RuntimeError(f"Not connected to ArduPilot SITL at {self.connection}. Is it running?")
            if self.v["armed"]:
                raise RuntimeError("The vehicle is already armed")
            self._reset_flight()
            self.abort_requested = False
            self.items = build()
            self.leg, self.task_seqs = leg, where
            self.leg_start_mah = self.v["consumed_mah"]
            self._waypoint_seqs = set(mm.nav_seqs(self.items))
            self.route_m = sum(math.dist(a, b) for a, b in zip(route_xy, route_xy[1:]))
            self.phase = "PREPARING"
            self._flight = threading.Thread(target=self._fly, name="sitl-flight", daemon=True)
            self._flight.start()

    def leg_progress(self) -> LegProgress:
        """Which tasks of the current leg are done, from the mission sequence.

        A task is done once the autopilot has moved past its service waypoint
        (the hold finished and it is climbing out), and active from when the
        autopilot heads for its approach waypoint until then.
        """
        p = LegProgress(finished=self.phase in ("DONE", "FAILED", "IDLE"), ok=self.phase != "FAILED", message=self.message)
        if self.leg is None:
            return p
        reached = set(self.reached)
        for tid in self.leg.task_ids:
            s = self.task_seqs[tid]
            if self._max_seq > s["service"] or s["depart"] in reached:
                p.done_tasks.append(tid)
            elif p.active_task is None and not self.abort_requested and self.v["ap_mode"] == "AUTO" and self._max_seq >= s["approach"]:
                p.active_task = tid
        p.flight_s = self.flight_s
        if self.leg_start_mah is not None and self.v["consumed_mah"] is not None:
            p.consumed_mah = float(self.v["consumed_mah"] - self.leg_start_mah)
        p.returning = self.abort_requested or (self.phase == "FLYING" and self.v["armed"] and self.v["ap_mode"] not in ("AUTO", "GUIDED"))
        return p

    def return_to_dock(self) -> None:
        self.abort()

    def set_charge_pct(self, pct: float) -> None:
        """The dock charger's simulated charge: set the battery monitor to pct.

        Same command as `recharge`, with a partial level. On a real vehicle the
        dock charges the pack and this must not be sent.
        """
        if self.master is None or self.flying:
            return
        self._inbox = queue.Queue(maxsize=500)
        self._listening.set()
        try:
            self._command(mavlink.MAV_CMD_BATTERY_RESET, 0xFF, round(pct))
        finally:
            self._listening.clear()

    def recharge(self) -> None:
        """Simulated charge: tell the autopilot's battery monitor the pack is full.

        MAV_CMD_BATTERY_RESET (ArduPilot) sets the remaining capacity, which is
        right for SITL. On a real vehicle the pack really is swapped or charged,
        and this should wait for the dock to report it full instead.
        """
        if self.master is None or self.flying:
            raise RuntimeError("Can only recharge on the dock while connected")
        self._inbox = queue.Queue(maxsize=500)
        self._listening.set()
        try:
            res = self._command(mavlink.MAV_CMD_BATTERY_RESET, 0xFF, 100)
        finally:
            self._listening.clear()
        if res == mavlink.MAV_RESULT_ACCEPTED:
            self.msglog.add("Battery monitor reset to 100% after the simulated charge", vitals.INFO, "planner")
        else:
            self.msglog.add(f"Autopilot did not accept BATTERY_RESET (result {res})", vitals.WARNING, "planner")

    def _fly(self) -> None:
        self._inbox = queue.Queue(maxsize=500)
        self._listening.set()
        try:
            self.message = f"Uploading mission ({len(self.items)} items)"
            self.msglog.add(self.message, vitals.INFO, "planner")
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
                    if self.leg is not None:
                        lp = self.leg_progress()
                        n = len(self.leg.task_ids)
                        where = f"{lp.active_task} in progress" if lp.active_task else "in transit"
                        if self._display_mode() == "LAND":
                            self.message = f"Leg {self.leg.index}: landing on the dock ({len(lp.done_tasks)} of {n} tasks done)"
                        elif self._display_mode() == "TAKEOFF":
                            self.message = f"Leg {self.leg.index}: taking off from the dock"
                        else:
                            self.message = f"Leg {self.leg.index}: {where}, {len(lp.done_tasks)} of {n} tasks done"
                    elif self._display_mode() == "LAND":
                        self.message = f"Landing on the dock ({len(self.reached)} of {total} waypoints reached)"
                    elif self._display_mode() == "TAKEOFF":
                        self.message = "Taking off from the dock"
                    else:
                        self.message = f"Surveying, {len(self.reached)} of {total} waypoints reached"
                time.sleep(TICK_S)
            self.phase = "DONE"
            self.message = "Aborted, landed at the dock" if self.abort_requested else "Mission complete, landed at the dock"
            self.msglog.add(self.message, vitals.NOTICE, "planner")
        except Exception as e:  # noqa: BLE001
            log.exception("SITL flight failed")
            self.phase = "FAILED"
            self.message = f"Flight failed: {e}"
            self.msglog.add(self.message, vitals.ERROR, "planner")
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
            self.msglog.add(self.message, vitals.WARNING, "planner")
            self.master.set_mode("RTL")  # fire and forget; the heartbeat shows whether it took

    def wait_done(self, timeout_s: float) -> bool:
        """Block until the current flight ends (for scripts and tests)."""
        t = self._flight
        if t is None:
            return True
        t.join(timeout_s)
        return not t.is_alive()
