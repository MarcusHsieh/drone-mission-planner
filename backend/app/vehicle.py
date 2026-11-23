"""The boundary between the planner and whatever flies.

The planner side (`tasks`, `energy`, `charge_plan`, `task_queue`) decides what
to fly and in what legs. A vehicle link only knows how to fly one leg, report
progress on it, go home, and recharge:

    link.start_leg(LegMission)   non-blocking; raises RuntimeError if it can't start
    link.flying                  True until the leg has ended (landed and disarmed)
    link.leg_progress()          LegProgress: which task is active, which are done
    link.return_to_dock()        cut the leg short and come home (RTL)
    link.recharge()              after landing: the pack is full again
    link.telemetry()             the dashboard's telemetry dict (battery, mode, ...)

Two links implement it today:

- `sim.DroneSim`: the built-in point-mass sim (FLIGHT_BACKEND=sim).
- `sitl_drone.SitlDrone`: ArduPilot over MAVLink (FLIGHT_BACKEND=sitl). It
  uploads the leg as a mission (`mavlink_mission.build_leg_mission`) and reads
  progress from MISSION_ITEM_REACHED / MISSION_CURRENT. Nothing in it is
  specific to SITL except `recharge`, so a real Pixhawk is the same class
  with a different SITL_CONNECTION (for example `serial:COM5:115200` or
  `udpin:0.0.0.0:14550`). On real hardware `recharge` must instead wait for
  the dock to report a full pack; see docs/concepts/task-queue.md.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Protocol

Role = Literal["transit", "approach", "service", "depart"]


@dataclass(frozen=True)
class LegWaypoint:
    x: float  # meters east of the dock
    y: float  # meters north of the dock
    alt_m: float  # above the dock (home)
    hold_s: float = 0.0
    task_id: str | None = None
    role: Role = "transit"


@dataclass(frozen=True)
class LegMission:
    """One flight: take off at the dock, fly the waypoints, land at the dock."""

    index: int
    waypoints: list[LegWaypoint]
    cruise_alt_m: float
    speed_ms: float

    @property
    def task_ids(self) -> list[str]:
        return [w.task_id for w in self.waypoints if w.role == "service" and w.task_id]


@dataclass
class LegProgress:
    active_task: str | None = None
    done_tasks: list[str] = field(default_factory=list)
    finished: bool = False  # landed and disarmed (or failed)
    ok: bool = True  # False if the vehicle reported a failure
    returning: bool = False  # cut short: RTL or abort
    message: str = ""
    # Measured by the vehicle for this leg, when it knows them.
    flight_s: float | None = None  # arm to disarm, vehicle (simulation) time
    consumed_mah: float | None = None


def leg_mission(index: int, stops: list[tuple[str, float, float, float]], cruise_alt_m: float, service_alt_m: float, speed_ms: float) -> LegMission:
    """A leg as waypoints: for each (task_id, x, y, hold_s), fly over it at
    cruise altitude, descend to the service altitude, hold, climb back."""
    wps: list[LegWaypoint] = []
    for tid, x, y, hold in stops:
        wps.append(LegWaypoint(x, y, cruise_alt_m, task_id=tid, role="approach"))
        wps.append(LegWaypoint(x, y, service_alt_m, hold_s=hold, task_id=tid, role="service"))
        wps.append(LegWaypoint(x, y, cruise_alt_m, task_id=tid, role="depart"))
    return LegMission(index, wps, cruise_alt_m, speed_ms)


class VehicleLink(Protocol):
    connected: bool

    @property
    def flying(self) -> bool: ...

    def start_leg(self, leg: LegMission) -> None: ...

    def leg_progress(self) -> LegProgress: ...

    def return_to_dock(self) -> None: ...

    def recharge(self) -> None: ...

    def telemetry(self) -> dict: ...
