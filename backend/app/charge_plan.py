"""Split a task queue into legs that each fit on one battery, with charge stops.

Route first, split second:

1. Order every pending task into one tour from the dock (nearest neighbor plus
   2-opt, `planner.plan_tour`).
2. Cut that tour into consecutive legs (dock -> some tasks -> dock) with an
   exact dynamic program over the cut points: fewest legs first, then least
   predicted energy. A leg is allowed only if `energy.estimate_leg` says it
   lands with at least the reserve left.
3. Re-order the stops inside each leg (it's a smaller tour now) and keep the
   better of the two orders.

Between legs the drone lands on the dock and charges. Tasks that can't be
reached even alone on a full battery are reported as unreachable.

The output is plain data (`LegPlan`): which tasks, in what order, the predicted
time and mAh, and the charge time after it. The vehicle link turns a leg into
a mission (`vehicle.leg_mission`); this module never talks to a vehicle.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from . import planner
from .energy import EnergyModel, LegEstimate, estimate_leg
from .tasks import Task

DOCK = (0.0, 0.0)


@dataclass
class LegPlan:
    index: int  # 1-based, in flying order
    task_ids: list[str]
    est: LegEstimate
    charge_after_s: float  # real-world seconds on the dock after this leg (0 for the last leg)
    end_pct: float  # predicted battery left on landing, % of the pack
    status: str = "planned"  # planned, flying, done, cut_short, aborted, failed
    actual: dict = field(default_factory=dict)  # filled in after the leg flies

    def to_json(self, model: EnergyModel, tasks: dict[str, Task], course=None) -> dict:
        path = [DOCK] + [(tasks[t].x, tasks[t].y) for t in self.task_ids] + [DOCK]
        out = {
            "index": self.index,
            "task_ids": self.task_ids,
            "distance_m": round(self.est.distance_m),
            "time_s": round(self.est.time_s),
            "mah": round(self.est.mah),
            "pct_of_pack": round(100 * self.est.mah / model.pack_mah, 1),
            "end_pct": round(self.end_pct, 1),
            "charge_after_s": round(self.charge_after_s),
            "status": self.status,
            "actual": self.actual,
            "breakdown_s": {k: round(v) for k, v in self.est.breakdown_s.items()},
        }
        if course is not None:
            out["path"] = [course.frame.to_lonlat(x, y) for x, y in path]
        return out


@dataclass
class QueuePlan:
    legs: list[LegPlan]
    unreachable: list[str]
    total_time_s: float  # flying plus charging, real-world
    total_mah: float


def _leg(model: EnergyModel, tasks: list[Task], service_alt_m: float) -> LegEstimate:
    return estimate_leg(model, [(t.x, t.y) for t in tasks], [t.service_s for t in tasks], service_alt_m)


def _reorder(model: EnergyModel, tasks: list[Task], service_alt_m: float) -> list[Task]:
    if len(tasks) < 3:
        return tasks
    idx, _, _ = planner.plan_tour([[[(t.x, t.y)]] for t in tasks], home=DOCK)
    alt = [tasks[i] for i in idx]
    return alt if _leg(model, alt, service_alt_m).mah < _leg(model, tasks, service_alt_m).mah - 1e-6 else tasks


def plan_queue(tasks: list[Task], model: EnergyModel, service_alt_m: float) -> QueuePlan:
    budget = model.usable_mah
    reachable, unreachable = [], []
    for t in tasks:
        (reachable if _leg(model, [t], service_alt_m).mah <= budget else unreachable).append(t)
    if not reachable:
        return QueuePlan([], [t.id for t in unreachable], 0.0, 0.0)

    order, _, _ = planner.plan_tour([[[(t.x, t.y)]] for t in reachable], home=DOCK)
    seq = [reachable[i] for i in order]
    n = len(seq)

    # best[j] = (legs, mah, cut) for covering seq[:j]; legs are seq[i:j].
    INF = (math.inf, math.inf, -1)
    best: list[tuple[float, float, int]] = [INF] * (n + 1)
    best[0] = (0, 0.0, -1)
    for i in range(n):
        if best[i][0] == math.inf:
            continue
        for j in range(i + 1, n + 1):
            e = _leg(model, seq[i:j], service_alt_m)
            if e.mah > budget:
                break  # longer legs from i only get more expensive
            cand = (best[i][0] + 1, best[i][1] + e.mah, i)
            if cand[:2] < best[j][:2]:
                best[j] = cand
    cuts = []
    j = n
    while j > 0:
        i = best[j][2]
        cuts.append((i, j))
        j = i
    cuts.reverse()

    legs: list[LegPlan] = []
    for k, (i, j) in enumerate(cuts):
        group = _reorder(model, seq[i:j], service_alt_m)
        est = _leg(model, group, service_alt_m)
        last = k == len(cuts) - 1
        legs.append(
            LegPlan(
                index=k + 1,
                task_ids=[t.id for t in group],
                est=est,
                charge_after_s=0.0 if last else model.charge_time_s(est.mah),
                end_pct=100 * (1 - est.mah / model.pack_mah),
            )
        )
    return QueuePlan(
        legs=legs,
        unreachable=[t.id for t in unreachable],
        total_time_s=sum(leg.est.time_s + leg.charge_after_s for leg in legs),
        total_mah=sum(leg.est.mah for leg in legs),
    )
