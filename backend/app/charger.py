"""Passive charging on the dock.

Whenever the drone sits on the dock (connected, landed, disarmed), the pack
charges, whether or not anything is queued. Flights don't trigger a charge;
they only wait for one when the pack is too low for what they are about to fly
(`wait_for`).

The rate comes from the energy model (`charge_c`, `charge_taper`, the
`charge_handling_s` settle time after landing) and runs `charge_speedup`
times faster than real time. It is simulated in both backends: the link's
`set_charge_pct` writes the new level into the vehicle (the sim's pack, or
ArduPilot's battery monitor through MAV_CMD_BATTERY_RESET). On a real dock the
dock reports the charge instead and this class only reads it.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import Any

from . import vitals
from .energy import EnergyModel

log = logging.getLogger(__name__)

POLL_S = 0.2
FULL_PCT = 99.5  # treat as full (vehicles report whole percents)


class DockCharger:
    def __init__(self, link, model: EnergyModel) -> None:
        self.link = link
        self.model = model
        self.state = "idle"  # idle (flying or disconnected), settling, charging, full
        self.pct: float | None = None  # our running estimate of the charge, percent
        self._landed_at: float | None = None
        self._pushed: float | None = None
        self._task: asyncio.Task | None = None
        self._push_lock = asyncio.Lock()

    # -- rates -----------------------------------------------------------------------
    def pct_per_wall_s(self) -> float:
        """Charge rate in percent per wall-clock second (speedup applied)."""
        full_h = self.model.charge_taper / max(self.model.charge_c, 1e-6)
        return 100 / (full_h * 3600) * self.model.charge_speedup

    def settle_wall_s(self) -> float:
        return self.model.charge_handling_s / max(self.model.charge_speedup, 1e-6)

    def wall_s_to(self, target_pct: float) -> float:
        """Wall-clock seconds until the pack reaches target_pct, if it stays on the dock."""
        now = self._reading()
        if now is None or now >= target_pct:
            return 0.0
        settle = 0.0
        if self.state != "charging":
            settle = self.settle_wall_s()
            if self._landed_at is not None:
                settle = max(0.0, settle - (time.monotonic() - self._landed_at))
        return settle + (target_pct - now) / self.pct_per_wall_s()

    # -- loop ------------------------------------------------------------------------
    def ensure_running(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    def _reading(self) -> float | None:
        if self.state == "charging" and self.pct is not None:
            return self.pct
        pct = self.link.telemetry().get("battery_pct")
        return None if pct is None else float(pct)

    def _on_dock(self) -> bool:
        if not getattr(self.link, "connected", True) or self.link.flying:
            return False
        tel = self.link.telemetry()
        return not tel.get("armed") and tel.get("battery_pct") is not None

    async def _loop(self) -> None:
        last = time.monotonic()
        while True:
            await asyncio.sleep(POLL_S)
            now = time.monotonic()
            dt, last = now - last, now
            try:
                await self._tick(now, dt)
            except Exception:  # noqa: BLE001
                log.exception("Dock charger tick failed")

    async def _tick(self, now: float, dt: float) -> None:
        if not self._on_dock():
            if self.state != "idle":
                self.state, self.pct, self._landed_at, self._pushed = "idle", None, None, None
            return
        if self._landed_at is None:
            self._landed_at = now
        if self.state == "idle":
            self.state = "settling"
        reading = float(self.link.telemetry()["battery_pct"])
        if self.state in ("settling", "full"):
            if reading >= FULL_PCT:
                self.state, self.pct = "full", reading
                return
            if self.state == "settling" and now - self._landed_at < self.settle_wall_s():
                return
            self.state, self.pct, self._pushed = "charging", reading, reading
            self._log(f"Charging on the dock from {reading:.0f}% (simulated {self.model.charge_speedup:g}x faster)")
        # charging
        assert self.pct is not None
        self.pct = min(100.0, self.pct + self.pct_per_wall_s() * dt)
        # Write whole-percent steps to the vehicle (SITL's battery reset takes whole percents).
        if self.pct >= 100.0 or int(self.pct) > int(self._pushed or 0):
            target = 100.0 if self.pct >= 100.0 else float(int(self.pct))
            async with self._push_lock:
                if self.link.flying:
                    return
                await asyncio.to_thread(self.link.set_charge_pct, target)
            self._pushed = target
        if self.pct >= 100.0:
            self.state = "full"
            self._log("Pack full on the dock")

    @contextlib.asynccontextmanager
    async def launching(self):
        """Hold charger writes while a flight starts (both talk to the vehicle)."""
        async with self._push_lock:
            yield

    async def wait_for(self, need_pct: float, what: str, on_wait=None, should_stop=None) -> bool:
        """Wait on the dock until the pack reads at least need_pct. Returns False if stopped."""
        self.ensure_running()
        need = min(need_pct, FULL_PCT)
        while True:
            pct = self.link.telemetry().get("battery_pct")
            if pct is None or pct >= need - 0.5:
                return True
            if should_stop is not None and should_stop():
                return False
            if on_wait is not None:
                on_wait(f"Waiting for the dock to charge {pct:.0f}% → {need:.0f}% before {what} "
                        f"(about {round(self.wall_s_to(need))} s, simulated)")
            await asyncio.sleep(POLL_S)

    def snapshot(self) -> dict[str, Any]:
        pct = self._reading()
        left_real_s = None
        if pct is not None and self.state in ("settling", "charging"):
            left_real_s = round(self.wall_s_to(100.0) * self.model.charge_speedup)
        return {"state": self.state, "pct": None if pct is None else round(pct, 1), "left_s": left_real_s}

    def _log(self, text: str) -> None:
        msglog = getattr(self.link, "msglog", None)
        if msglog is not None:
            msglog.add(text, vitals.INFO, "planner")


def need_pct(model: EnergyModel, flight_mah: float) -> float:
    """Charge a flight needs at takeoff: its estimate plus the landing reserve, as a percent of the pack."""
    return min(100.0, 100 * flight_mah / model.pack_mah + model.reserve_pct)
