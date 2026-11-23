"""Turn a planner route into an ArduPilot mission and upload it over MAVLink.

The planner's route is a list of (x, y) points in the dock frame that starts
and ends at the dock. As a Copter mission that becomes:

    seq 0  home (the dock; ArduPilot overwrites it with where it armed)
    seq 1  NAV_TAKEOFF to ALT_M
    seq 2  DO_CHANGE_SPEED to the planner's cruise speed
    seq 3+ NAV_WAYPOINT for every route point between the two dock visits
    last   NAV_LAND at the dock

Altitudes are relative to home, so the dock's height above sea level does not
matter. `build_mission` is pure so it can be tested without a vehicle; the
upload functions speak the MAVLink mission protocol (MISSION_COUNT, then the
vehicle asks for each item with MISSION_REQUEST_INT) on a pymavlink connection.
"""

from __future__ import annotations

import math
import os
import time
from dataclasses import dataclass
from typing import Callable

# MAVLink 2 (ArduPilot's default); must be set before pymavlink is imported.
os.environ.setdefault("MAVLINK20", "1")

from pymavlink import mavutil  # noqa: E402

from . import planner
from .geo import LocalFrame
from .vehicle import LegMission

mavlink = mavutil.mavlink

ALT_M = planner.ALTITUDE_M
SPEED_MS = planner.CRUISE_SPEED_MS
MIN_LEG_M = 0.5  # drop route points closer than this to the previous one


@dataclass(frozen=True)
class MissionItem:
    seq: int
    command: int
    frame: int
    lat: float = 0.0
    lon: float = 0.0
    alt: float = 0.0
    params: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)

    @property
    def is_nav_waypoint(self) -> bool:
        return self.command == mavlink.MAV_CMD_NAV_WAYPOINT

    def to_msg(self, target_system: int, target_component: int):
        return mavlink.MAVLink_mission_item_int_message(
            target_system,
            target_component,
            self.seq,
            self.frame,
            self.command,
            0,  # current
            1,  # autocontinue
            *self.params,
            int(round(self.lat * 1e7)),
            int(round(self.lon * 1e7)),
            self.alt,
            mavlink.MAV_MISSION_TYPE_MISSION,
        )


def build_mission(
    route_xy: list[tuple[float, float]],
    frame: LocalFrame,
    alt_m: float = ALT_M,
    speed_ms: float = SPEED_MS,
) -> list[MissionItem]:
    """Mission items for a dock -> survey -> dock route (see module docstring)."""
    if len(route_xy) < 2:
        raise ValueError("A route needs at least a start and an end")
    rel = mavlink.MAV_FRAME_GLOBAL_RELATIVE_ALT
    dock_lon, dock_lat = frame.to_lonlat(*route_xy[-1])
    home_lon, home_lat = frame.to_lonlat(*route_xy[0])

    survey: list[tuple[float, float]] = []
    last = route_xy[0]
    for p in route_xy[1:-1]:
        if math.dist(p, last) >= MIN_LEG_M:
            survey.append(p)
            last = p

    items = [
        MissionItem(0, mavlink.MAV_CMD_NAV_WAYPOINT, mavlink.MAV_FRAME_GLOBAL, home_lat, home_lon, 0.0),
        MissionItem(1, mavlink.MAV_CMD_NAV_TAKEOFF, rel, 0.0, 0.0, alt_m),
        # param1 1 = groundspeed, param2 = m/s, param3 -1 = leave throttle alone
        MissionItem(2, mavlink.MAV_CMD_DO_CHANGE_SPEED, rel, params=(1.0, speed_ms, -1.0, 0.0)),
    ]
    for x, y in survey:
        lon, lat = frame.to_lonlat(x, y)
        items.append(MissionItem(len(items), mavlink.MAV_CMD_NAV_WAYPOINT, rel, lat, lon, alt_m))
    items.append(MissionItem(len(items), mavlink.MAV_CMD_NAV_LAND, rel, dock_lat, dock_lon, 0.0))
    return items


def build_leg_mission(leg: LegMission, frame: LocalFrame) -> tuple[list[MissionItem], dict[str, dict[str, int]]]:
    """Mission items for one task-queue leg, plus where each task is in it.

    Same frame as `build_mission` (home, takeoff, speed, waypoints, land at the
    dock), but every waypoint keeps its own altitude and hold time (param1 of
    NAV_WAYPOINT, seconds). A task is three waypoints: approach (over it at
    cruise altitude), service (down at the service altitude, with the hold)
    and depart (back up). Returns {task_id: {"approach": seq, "service": seq,
    "depart": seq}} so progress can be read from the mission sequence.
    """
    rel = mavlink.MAV_FRAME_GLOBAL_RELATIVE_ALT
    dock_lon, dock_lat = frame.to_lonlat(0.0, 0.0)
    items = [
        MissionItem(0, mavlink.MAV_CMD_NAV_WAYPOINT, mavlink.MAV_FRAME_GLOBAL, dock_lat, dock_lon, 0.0),
        MissionItem(1, mavlink.MAV_CMD_NAV_TAKEOFF, rel, 0.0, 0.0, leg.cruise_alt_m),
        MissionItem(2, mavlink.MAV_CMD_DO_CHANGE_SPEED, rel, params=(1.0, leg.speed_ms, -1.0, 0.0)),
    ]
    where: dict[str, dict[str, int]] = {}
    for w in leg.waypoints:
        lon, lat = frame.to_lonlat(w.x, w.y)
        seq = len(items)
        items.append(MissionItem(seq, mavlink.MAV_CMD_NAV_WAYPOINT, rel, lat, lon, w.alt_m, params=(float(w.hold_s), 0.0, 0.0, 0.0)))
        if w.task_id:
            where.setdefault(w.task_id, {})[w.role] = seq
    items.append(MissionItem(len(items), mavlink.MAV_CMD_NAV_LAND, rel, dock_lat, dock_lon, 0.0))
    return items, where


def nav_seqs(items: list[MissionItem]) -> list[int]:
    """Sequence numbers of the survey waypoints.

    Copter sends MISSION_ITEM_REACHED for each of these. It also sends one for
    the takeoff but none for NAV_LAND, so the landing is checked by position
    and disarm instead.
    """
    return [it.seq for it in items if it.seq > 0 and it.command == mavlink.MAV_CMD_NAV_WAYPOINT]


class MissionUploadError(RuntimeError):
    pass


# A function that waits for one of the given message types, or returns None on timeout.
Recv = Callable[[list[str], float], object | None]


def upload_mission(master, items: list[MissionItem], recv: Recv, timeout_s: float = 15.0) -> None:
    """Send `items` to the vehicle with the MAVLink mission protocol.

    `recv` reads the next message of the given types (so a reader thread can
    own the socket). Raises MissionUploadError if the vehicle rejects the
    mission or stops asking for items.
    """
    ts, tc = master.target_system, master.target_component
    master.mav.mission_count_send(ts, tc, len(items), mavlink.MAV_MISSION_TYPE_MISSION)
    deadline = time.monotonic() + timeout_s
    sent: set[int] = set()
    while time.monotonic() < deadline:
        msg = recv(["MISSION_REQUEST_INT", "MISSION_REQUEST", "MISSION_ACK"], 2.0)
        if msg is None:
            if not sent:  # the count got lost: send it again
                master.mav.mission_count_send(ts, tc, len(items), mavlink.MAV_MISSION_TYPE_MISSION)
            continue
        if getattr(msg, "mission_type", 0) != mavlink.MAV_MISSION_TYPE_MISSION:
            continue
        if msg.get_type() == "MISSION_ACK":
            if msg.type == mavlink.MAV_MISSION_ACCEPTED and len(sent) == len(items):
                return
            if msg.type != mavlink.MAV_MISSION_ACCEPTED:
                raise MissionUploadError(f"Vehicle rejected the mission (MAV_MISSION_RESULT {msg.type})")
            continue
        if not 0 <= msg.seq < len(items):
            raise MissionUploadError(f"Vehicle asked for item {msg.seq} of {len(items)}")
        master.mav.send(items[msg.seq].to_msg(ts, tc))
        sent.add(msg.seq)
        deadline = time.monotonic() + timeout_s
    raise MissionUploadError(f"Mission upload timed out after sending {len(sent)} of {len(items)} items")


def download_mission(master, recv: Recv, timeout_s: float = 15.0) -> list:
    """Read the mission back from the vehicle (MISSION_ITEM_INT messages, in order)."""
    ts, tc = master.target_system, master.target_component
    master.mav.mission_request_list_send(ts, tc, mavlink.MAV_MISSION_TYPE_MISSION)
    msg = recv(["MISSION_COUNT"], timeout_s)
    if msg is None:
        raise MissionUploadError("No MISSION_COUNT from vehicle")
    out = []
    for seq in range(msg.count):
        for _ in range(3):
            master.mav.mission_request_int_send(ts, tc, seq, mavlink.MAV_MISSION_TYPE_MISSION)
            item = recv(["MISSION_ITEM_INT"], 2.0)
            if item is not None and item.seq == seq:
                out.append(item)
                break
        else:
            raise MissionUploadError(f"Vehicle did not send item {seq}")
    master.mav.mission_ack_send(ts, tc, mavlink.MAV_MISSION_ACCEPTED, mavlink.MAV_MISSION_TYPE_MISSION)
    return out
