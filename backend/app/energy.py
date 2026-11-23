"""Battery energy model for planning legs and charge stops.

A deliberately simple, conservative model: the drone draws a fixed current
in each flight phase (hover, cruise, climb, descent), and every prediction is
multiplied by a safety margin. Nothing here is calibrated. The defaults are
placeholders for a Holybro X500 V2 class quad on a 4S ~5000 mAh pack, picked
on the high-draw side so the planner errs toward more charge stops, not fewer.
Replace them with numbers measured on the real airframe (hover current from a
logged hover, cruise current from a logged survey leg) before trusting any
flight time this model prints.

Every number can be overridden with an ENERGY_* or CHARGE_* environment
variable (see `EnergyModel.from_env`), so a different pack or a calibrated
airframe needs no code change.
"""

from __future__ import annotations

import math
import os
from dataclasses import asdict, dataclass, field

from . import planner


def _env(name: str, default: float) -> float:
    raw = os.getenv(name)
    return float(raw) if raw not in (None, "") else default


@dataclass(frozen=True)
class EnergyModel:
    # Pack: 4S LiPo, nominal capacity. Usable energy is capacity minus the reserve.
    pack_mah: float = 5000.0
    cells: int = 4
    reserve_pct: float = 30.0  # land every leg with at least this much left (by the model)
    margin: float = 1.15  # every predicted mAh and second is multiplied by this

    # Current draw per phase (A). Placeholders, not measurements.
    hover_a: float = 20.0
    cruise_a: float = 20.0  # forward flight at cruise speed; assumed no better than hover
    climb_a: float = 26.0
    descent_a: float = 16.0
    idle_a: float = 0.5  # armed on the ground, spooling up

    # Kinematics (match the mission the autopilot flies).
    cruise_ms: float = planner.CRUISE_SPEED_MS
    climb_ms: float = 2.5  # ArduPilot WPNAV_SPEED_UP default
    descent_ms: float = 1.5  # ArduPilot WPNAV_SPEED_DN default
    land_ms: float = 0.5  # ArduPilot LAND_SPEED default (final 10 m of a landing)
    cruise_alt_m: float = planner.ALTITUDE_M
    wp_overhead_s: float = 3.0  # accelerating and braking at each waypoint
    leg_overhead_s: float = 20.0  # arming, spool-up, settling before takeoff and after touchdown

    # Charging on the dock.
    charge_c: float = 1.0  # charge rate as a multiple of capacity (1C = 5 A for 5000 mAh)
    charge_taper: float = 1.15  # constant-voltage tail at the end of a charge
    charge_handling_s: float = 120.0  # docking, contacts, balance check
    charge_speedup: float = 60.0  # simulation only: charging runs this many times faster than real time

    @classmethod
    def from_env(cls) -> EnergyModel:
        d = cls()
        names = {
            "pack_mah": "ENERGY_PACK_MAH",
            "reserve_pct": "ENERGY_RESERVE_PCT",
            "margin": "ENERGY_MARGIN",
            "hover_a": "ENERGY_HOVER_A",
            "cruise_a": "ENERGY_CRUISE_A",
            "climb_a": "ENERGY_CLIMB_A",
            "descent_a": "ENERGY_DESCENT_A",
            "charge_c": "CHARGE_C",
            "charge_speedup": "CHARGE_SPEEDUP",
        }
        kw = {k: _env(env, getattr(d, k)) for k, env in names.items()}
        return cls(**kw)

    # -- derived ---------------------------------------------------------------
    @property
    def usable_mah(self) -> float:
        """Energy one leg may plan to use: the pack minus the landing reserve."""
        return self.pack_mah * (1 - self.reserve_pct / 100)

    def charge_time_s(self, used_mah: float) -> float:
        """Real-world time on the dock to put `used_mah` back (before charge_speedup)."""
        if used_mah <= 0:
            return 0.0
        hours = used_mah / (self.charge_c * self.pack_mah) * self.charge_taper
        return hours * 3600 + self.charge_handling_s

    def describe(self) -> dict:
        out = asdict(self)
        out["usable_mah"] = round(self.usable_mah)
        out["hover_minutes_to_reserve"] = round(self.usable_mah / (self.hover_a * self.margin) * 60 / 1000, 1)
        out["calibrated"] = False
        return out


@dataclass
class LegEstimate:
    """Time and energy for one dock -> tasks -> dock leg, with a breakdown."""

    distance_m: float = 0.0
    time_s: float = 0.0
    mah: float = 0.0
    breakdown_s: dict[str, float] = field(default_factory=dict)

    def add(self, phase: str, seconds: float, amps: float) -> None:
        self.time_s += seconds
        self.mah += amps * seconds / 3.6  # A * s -> mAh
        self.breakdown_s[phase] = self.breakdown_s.get(phase, 0.0) + seconds


def estimate_leg(
    model: EnergyModel,
    stops: list[tuple[float, float]],
    service_s: float | list[float],
    service_alt_m: float,
    home: tuple[float, float] = (0.0, 0.0),
) -> LegEstimate:
    """Predict a leg: take off at `home`, visit every stop in order (descend to
    `service_alt_m`, hold `service_s` (one value, or one per stop), climb back), return and land.

    The result already includes `model.margin`.
    """
    e = LegEstimate()
    alt = model.cruise_alt_m
    drop = max(alt - service_alt_m, 0.0)
    e.add("ground", model.leg_overhead_s, model.idle_a)
    e.add("climb", alt / model.climb_ms, model.climb_a)
    holds = service_s if isinstance(service_s, list) else [service_s] * len(stops)
    here = home
    for p, hold in zip(stops, holds):
        d = math.dist(here, p)
        e.distance_m += d
        e.add("cruise", d / model.cruise_ms + model.wp_overhead_s, model.cruise_a)
        e.add("descent", drop / model.descent_ms, model.descent_a)
        e.add("service", hold, model.hover_a)
        e.add("climb", drop / model.climb_ms, model.climb_a)
        here = p
    d = math.dist(here, home)
    e.distance_m += d
    e.add("cruise", d / model.cruise_ms + model.wp_overhead_s, model.cruise_a)
    # Copter lands at WPNAV_SPEED_DN down to 10 m, then LAND_SPEED.
    fast = max(alt - 10.0, 0.0)
    e.add("land", fast / model.descent_ms + min(alt, 10.0) / model.land_ms, model.descent_a)
    e.time_s *= model.margin
    e.mah *= model.margin
    e.breakdown_s = {k: v * model.margin for k, v in e.breakdown_s.items()}
    return e
