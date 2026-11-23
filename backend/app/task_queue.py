"""The task queue and the loop that works through it, leg by leg.

    tasks (pending) -> charge_plan.plan_queue -> first leg -> vehicle link
          ^                                                       |
          |  leftovers back to pending        land, charge <------+

Only the first leg of each plan is flown. After it lands, whatever is still
pending is planned again from scratch, so a leg that was cut short (abort,
low battery) or flew differently from the prediction never leaves the plan
stale. Measured flight time and mAh are kept next to the prediction for each
leg, which is the data needed to calibrate the energy model later.

The battery guard: if the vehicle's own battery reading drops below
GUARD_PCT during a leg, the leg is cut short and the drone returns to the
dock. The plan lands with ENERGY_RESERVE_PCT left, so the guard only fires
when the model was too optimistic. The autopilot's own battery failsafe stays
the last line of defense on a real vehicle.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any

from . import vitals
from .charge_plan import LegPlan, QueuePlan, plan_queue
from .course import Course
from .energy import EnergyModel
from .tasks import MAX_ATTEMPTS, Task, generate_divots
from .vehicle import LegProgress, VehicleLink, leg_mission

log = logging.getLogger(__name__)

SERVICE_ALT_M = float(os.getenv("DIVOT_SERVICE_ALT_M", "4"))
SERVICE_S = float(os.getenv("DIVOT_SERVICE_S", "15"))
GUARD_PCT = float(os.getenv("QUEUE_GUARD_PCT", "20"))
TOPUP_BELOW_PCT = 97.0  # charge before a leg if the pack reads less than this
POLL_S = 0.2


class TaskQueue:
    def __init__(self, course: Course, link: VehicleLink, model: EnergyModel | None = None) -> None:
        self.course = course
        self.link = link
        self.model = model or EnergyModel.from_env()
        self.service_alt_m = SERVICE_ALT_M
        self.tasks: dict[str, Task] = {}
        self.plan: QueuePlan | None = None
        self.flown: list[LegPlan] = []
        self.phase = "idle"  # idle, flying, charging, done, aborted, failed
        self.message = "No tasks. Generate divots to fill the queue."
        self.charge: dict[str, float] | None = None
        self.abort_requested = False
        self.version = 0
        self._runner: asyncio.Task | None = None
        self._next_id = 1

    @property
    def running(self) -> bool:
        return self._runner is not None and not self._runner.done()

    def _changed(self) -> None:
        self.version += 1

    def _log(self, text: str, severity: int = vitals.INFO) -> None:
        msglog = getattr(self.link, "msglog", None)
        if msglog is not None:
            msglog.add(text, severity, "planner")
        log.info(text)

    # -- editing the queue ---------------------------------------------------------
    def generate(self, count: int, holes: list[int] | None = None, seed: int | None = None, replace: bool = True, service_s: float = SERVICE_S) -> None:
        if self.running:
            raise RuntimeError("The queue is running. Abort it first.")
        if replace:
            self.tasks, self.flown, self._next_id = {}, [], 1
        new = generate_divots(self.course, count, holes, seed, service_s, self._next_id)
        self._next_id += len(new)
        self.tasks.update({t.id: t for t in new})
        self.phase = "idle"
        self.replan()
        self.message = f"{len(new)} divots added. {self._plan_summary()}"

    def clear(self) -> None:
        if self.running:
            raise RuntimeError("The queue is running. Abort it first.")
        self.tasks, self.flown, self.plan, self._next_id = {}, [], None, 1
        self.phase, self.charge = "idle", None
        self.message = "No tasks. Generate divots to fill the queue."
        self._changed()

    def replan(self) -> QueuePlan:
        pending = [t for t in self.tasks.values() if t.status == "pending"]
        self.plan = plan_queue(pending, self.model, self.service_alt_m)
        for tid in self.plan.unreachable:
            t = self.tasks[tid]
            t.status, t.note = "failed", "Out of range: can't reach it and return with the reserve on one battery"
        base = len(self.flown)
        for leg in self.plan.legs:
            leg.index += base
            for tid in leg.task_ids:
                self.tasks[tid].leg = leg.index
        self._changed()
        return self.plan

    def _plan_summary(self) -> str:
        p = self.plan
        if not p or not p.legs:
            return "Nothing left to fly."
        stops = len(p.legs) - 1
        return f"{len(p.legs)} leg{'s' if len(p.legs) != 1 else ''}, {stops} charge stop{'s' if stops != 1 else ''}."

    # -- running ---------------------------------------------------------------------
    def start(self) -> None:
        if self.running:
            raise RuntimeError("Already running")
        if not any(t.status == "pending" for t in self.tasks.values()):
            raise RuntimeError("No pending tasks. Generate divots first.")
        if self.link.flying:
            raise RuntimeError("The drone is already flying")
        if not getattr(self.link, "connected", True):
            raise RuntimeError("The vehicle is not connected")
        self.abort_requested = False
        self.phase = "flying"
        self._runner = asyncio.create_task(self._run())

    def abort(self) -> None:
        if not self.running:
            raise RuntimeError("Not running")
        self.abort_requested = True
        if self.link.flying:
            self.link.return_to_dock()
        self.message = "Abort: returning to the dock, the queue stops after landing"
        self._changed()

    async def _run(self) -> None:
        try:
            if self.plan is None:
                self.replan()
            while True:
                leg = next((x for x in self.plan.legs if x.status == "planned"), None)
                if leg is None:
                    break
                # Every leg is planned for a full pack: charge first if it isn't full.
                await self._top_up(f"before leg {leg.index}")
                if self.abort_requested:
                    self.phase = "aborted"
                    self.message = "Aborted while charging. Remaining tasks are pending."
                    return
                ok = await self._fly_leg(leg)
                if not ok:
                    self.phase = "failed"
                    return
                if self.abort_requested:
                    self.phase = "aborted"
                    self.message = "Aborted. The drone is on the dock; remaining tasks are pending."
                    self._log(self.message, vitals.WARNING)
                    return
                if leg.status != "done":
                    # Tasks came back to the queue: plan what's left again.
                    self.replan()
            self.replan()
            done = sum(t.status == "done" for t in self.tasks.values())
            failed = sum(t.status == "failed" for t in self.tasks.values())
            self.phase = "done"
            self.message = f"Queue finished: {done} done, {failed} failed, in {len(self.flown)} leg{'s' if len(self.flown) != 1 else ''}."
            self._log(self.message, vitals.NOTICE)
        except Exception as e:  # noqa: BLE001
            log.exception("Task queue failed")
            self.phase = "failed"
            self.message = f"Queue stopped: {e}"
            self._log(self.message, vitals.ERROR)
        finally:
            self.charge = None
            for t in self.tasks.values():
                if t.status == "active":
                    t.status = "pending"
            self.replan()  # whatever is still pending gets a fresh plan for the next run

    async def _fly_leg(self, leg: LegPlan) -> bool:
        stops = [(tid, self.tasks[tid].x, self.tasks[tid].y, self.tasks[tid].service_s) for tid in leg.task_ids]
        mission = leg_mission(leg.index, stops, self.model.cruise_alt_m, self.service_alt_m, self.model.cruise_ms)
        for tid in leg.task_ids:
            self.tasks[tid].attempts += 1
        leg.status = "flying"
        self.phase = "flying"
        self.message = f"Leg {leg.index}: {len(leg.task_ids)} tasks, predicted {round(leg.est.mah)} mAh ({round(leg.est.time_s / 60, 1)} min)"
        self._log(self.message)
        self.link.start_leg(mission)
        guard_fired = False
        p = LegProgress()
        while True:
            p = self.link.leg_progress()
            self._apply(leg, p)
            n_done = sum(self.tasks[tid].status == "done" for tid in leg.task_ids)
            doing = f"{p.active_task} in progress" if p.active_task else "returning to the dock" if p.returning else "in transit"
            msg = f"Leg {leg.index}: {n_done} of {len(leg.task_ids)} tasks done, {doing}"
            if msg != self.message and not self.abort_requested:
                self.message = msg
                self._changed()
            tel = self.link.telemetry()
            if not guard_fired and not p.returning and tel.get("armed") and tel.get("battery_pct", 100) < GUARD_PCT:
                guard_fired = True
                self._log(f"Leg {leg.index}: battery {tel['battery_pct']:.0f}% is under the {GUARD_PCT:.0f}% guard, returning to the dock early", vitals.WARNING)
                self.link.return_to_dock()
            if p.finished and not self.link.flying:
                break
            await asyncio.sleep(POLL_S)
        p = self.link.leg_progress()
        self._apply(leg, p)
        done = [tid for tid in leg.task_ids if self.tasks[tid].status == "done"]
        leg.actual = {
            "tasks_done": len(done),
            "flight_s": None if p.flight_s is None else round(p.flight_s),
            "mah": None if p.consumed_mah is None else round(p.consumed_mah),
            "landed_from_dock_m": self.link.telemetry().get("dock_distance_m"),
        }
        if p.consumed_mah and leg.est.mah:
            leg.actual["mah_vs_plan"] = round(p.consumed_mah / leg.est.mah, 2)
        for tid in leg.task_ids:
            t = self.tasks[tid]
            if t.status != "done":
                if t.attempts >= MAX_ATTEMPTS:
                    t.status, t.note = "failed", f"Not completed after {t.attempts} tries"
                else:
                    t.status, t.note = "pending", "Leg ended before this task; it will be planned again"
        if not p.ok:
            leg.status = "failed"
            self.message = f"Leg {leg.index} failed: {p.message}"
        elif self.abort_requested:
            leg.status = "aborted"
        elif guard_fired or len(done) < len(leg.task_ids):
            leg.status = "cut_short"
        else:
            leg.status = "done"
        self.flown.append(leg)
        self._log(f"Leg {leg.index} {leg.status.replace('_', ' ')}: {len(done)} of {len(leg.task_ids)} tasks done, "
                  f"{leg.actual['mah'] if leg.actual['mah'] is not None else '?'} mAh used (plan {round(leg.est.mah)})")  # fmt: skip
        self._changed()
        return p.ok

    def _apply(self, leg: LegPlan, p: LegProgress) -> None:
        changed = False
        for tid in leg.task_ids:
            t = self.tasks[tid]
            want = "done" if tid in p.done_tasks else "active" if tid == p.active_task else t.status if t.status != "active" else "pending"
            if want != t.status:
                t.status, t.leg = want, leg.index
                changed = True
                if want == "done":
                    t.note = ""
        if changed:
            self._changed()

    async def _top_up(self, when: str) -> None:
        """Charge on the dock until the pack is full (simulated: a wait, then link.recharge)."""
        tel = self.link.telemetry()
        pct = tel.get("battery_pct")
        if pct is None or pct >= TOPUP_BELOW_PCT:
            return
        # By percentage, so the time is for the model's pack even when the
        # vehicle's own pack differs (SITL's is a placeholder).
        used = self.model.pack_mah * (100 - pct) / 100
        real_s = self.model.charge_time_s(used)
        wall_s = real_s / max(self.model.charge_speedup, 1e-6)
        self.phase = "charging"
        self.charge = {"real_s": round(real_s), "wall_s": round(wall_s, 1), "left_s": round(real_s), "pct": 0.0}
        self._log(f"Charging on the dock {when}: {round(used)} mAh back in about {round(real_s / 60)} min (simulated {self.model.charge_speedup:g}x faster)")
        t0 = time.monotonic()
        while (el := time.monotonic() - t0) < wall_s:
            if self.abort_requested:
                return
            frac = el / wall_s
            self.charge.update(left_s=round(real_s * (1 - frac)), pct=round(100 * frac, 1))
            self.message = f"Charging on the dock {when}, {self.charge['pct']:.0f}% ({round(self.charge['left_s'] / 60)} min left, simulated)"
            self._changed()
            await asyncio.sleep(POLL_S)
        await asyncio.to_thread(self.link.recharge)
        # Give the vehicle a moment to report the full pack before the next check.
        for _ in range(25):
            if (self.link.telemetry().get("battery_pct") or 0) >= TOPUP_BELOW_PCT:
                break
            await asyncio.sleep(POLL_S)
        self.charge = None
        self._changed()

    # -- reading ---------------------------------------------------------------------
    def snapshot(self) -> dict[str, Any]:
        tasks = list(self.tasks.values())
        counts = {s: sum(t.status == s for t in tasks) for s in ("pending", "active", "done", "failed")}
        flown = {id(leg) for leg in self.flown}
        current = [leg for leg in (self.plan.legs if self.plan else []) if id(leg) not in flown]
        remaining = [leg for leg in current if leg.status == "planned"]
        legs = [leg.to_json(self.model, self.tasks, self.course) for leg in self.flown + current]
        return {
            "version": self.version,
            "phase": self.phase,
            "running": self.running,
            "message": self.message,
            "counts": counts,
            "tasks": [t.to_json(self.course) for t in tasks],
            "legs": legs,
            "remaining": {
                "legs": len(remaining),
                # Every remaining leg needs a charge first, except the very first leg of the queue.
                "charge_stops": max(len(remaining) - (0 if self.flown else 1), 0),
                "time_s": round(sum(leg.est.time_s + leg.charge_after_s for leg in remaining)),
                "mah": round(sum(leg.est.mah for leg in remaining)),
            },
            "charge": self.charge,
            "energy": self.model.describe(),
            "service": {"alt_m": self.service_alt_m, "guard_pct": GUARD_PCT},
        }
