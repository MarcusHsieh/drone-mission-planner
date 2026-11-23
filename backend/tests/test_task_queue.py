import asyncio
import dataclasses
import time

from fastapi.testclient import TestClient

from app import config
from app import main as appmain
from app.charge_plan import plan_queue
from app.course import Course
from app.energy import EnergyModel, estimate_leg
from app.geo import LocalFrame
from app.mavlink_mission import build_leg_mission, mavlink
from app.sim import DroneSim
from app.task_queue import TaskQueue
from app.tasks import generate_divots
from app.vehicle import leg_mission

COURSE = Course()


def test_divots_are_on_fairways_and_spaced():
    ts = generate_divots(COURSE, 30, seed=3)
    assert len(ts) == 30 and len({t.id for t in ts}) == 30
    assert all(t.hole is not None and t.status == "pending" for t in ts)
    for a in ts:
        for b in ts:
            if a is not b:
                assert (a.x - b.x) ** 2 + (a.y - b.y) ** 2 >= 6.0**2 - 1e-6
    only4 = generate_divots(COURSE, 5, holes=[4], seed=1)
    assert {t.hole for t in only4} == {4}


def test_leg_estimate_grows_with_stops_and_includes_margin():
    m = EnergyModel()
    one = estimate_leg(m, [(100.0, 0.0)], 15.0, 4.0)
    two = estimate_leg(m, [(100.0, 0.0), (100.0, 100.0)], 15.0, 4.0)
    assert two.mah > one.mah and two.time_s > one.time_s
    plain = estimate_leg(dataclasses.replace(m, margin=1.0), [(100.0, 0.0)], 15.0, 4.0)
    assert abs(one.mah - plain.mah * m.margin) < 1e-6
    assert abs(one.distance_m - 200.0) < 1e-6


def test_plan_splits_into_legs_within_budget():
    m = EnergyModel()
    ts = generate_divots(COURSE, 40, seed=7)
    p = plan_queue(ts, m, 4.0)
    assert len(p.legs) >= 2, "40 divots should not fit on one 5000 mAh battery"
    flat = [tid for leg in p.legs for tid in leg.task_ids]
    assert sorted(flat) == sorted(t.id for t in ts) and not p.unreachable
    for leg in p.legs:
        assert leg.est.mah <= m.usable_mah + 1e-6
        assert leg.end_pct >= m.reserve_pct - 1e-6
    assert p.legs[-1].charge_after_s == 0 and all(leg.charge_after_s > 0 for leg in p.legs[:-1])


def test_unreachable_task_is_reported():
    tiny = dataclasses.replace(EnergyModel(), pack_mah=300)
    ts = generate_divots(COURSE, 10, seed=2)
    p = plan_queue(ts, tiny, 4.0)
    assert p.unreachable  # some holes are too far for a 300 mAh pack
    assert set(p.unreachable).isdisjoint(tid for leg in p.legs for tid in leg.task_ids)


def test_leg_mission_items_and_task_seqs():
    leg = leg_mission(1, [("D01", 50.0, 20.0, 15.0), ("D02", 80.0, -10.0, 10.0)], 15.0, 4.0, 5.0)
    items, where = build_leg_mission(leg, LocalFrame(37.7, -121.9))
    assert items[1].command == mavlink.MAV_CMD_NAV_TAKEOFF and items[-1].command == mavlink.MAV_CMD_NAV_LAND
    assert where["D01"] == {"approach": 3, "service": 4, "depart": 5}
    svc = items[where["D02"]["service"]]
    assert svc.alt == 4.0 and svc.params[0] == 10.0
    assert items[where["D02"]["depart"]].alt == 15.0


def _run_queue(model: EnergyModel, n: int, seed: int, abort_after_s: float | None = None, holes: list[int] | None = None, vehicle: EnergyModel | None = None) -> TaskQueue:
    async def go() -> TaskQueue:
        sim = DroneSim(COURSE.frame, vehicle or model)
        q = TaskQueue(COURSE, sim, model)
        q.generate(n, holes=holes, seed=seed)
        q.start()
        t0 = time.monotonic()
        while q.running:
            if abort_after_s is not None and time.monotonic() - t0 > abort_after_s and not q.abort_requested:
                q.abort()
            assert time.monotonic() - t0 < 60, q.message
            await asyncio.sleep(0.05)
        return q

    return asyncio.run(go())


def test_queue_flies_every_leg_and_charges_between(monkeypatch):
    monkeypatch.setattr(config, "SIM_SPEEDUP", 400.0)
    # A smaller pack and nearby holes: several legs, all reachable, and quick to fly.
    model = dataclasses.replace(EnergyModel(), pack_mah=2500, charge_speedup=1e6)
    q = _run_queue(model, 12, seed=5, holes=[1, 2, 3])
    s = q.snapshot()
    assert s["phase"] == "done", s["message"]
    assert s["counts"]["done"] == 12 and s["counts"]["failed"] == 0
    legs = s["legs"]
    assert len(legs) >= 2 and all(leg["status"] == "done" for leg in legs)
    for leg in legs:
        # The sim draws the model's currents without the margin, so it lands under the prediction.
        assert 0 < leg["actual"]["mah"] <= leg["mah"]
    assert q.link.battery > 100 * (1 - model.usable_mah / model.pack_mah) - 1


def test_battery_guard_cuts_a_leg_short_and_replans(monkeypatch):
    """The real drone draws far more than the model: the guard brings it home early,
    unfinished tasks go back to pending and are flown on later legs."""
    monkeypatch.setattr(config, "SIM_SPEEDUP", 400.0)
    plan = dataclasses.replace(EnergyModel(), charge_speedup=1e6)
    hungry = dataclasses.replace(plan, hover_a=60.0, cruise_a=60.0, climb_a=70.0, descent_a=50.0)
    q = _run_queue(plan, 12, seed=11, holes=[3, 4, 5], vehicle=hungry)
    s = q.snapshot()
    assert any(leg["status"] == "cut_short" for leg in s["legs"]), [leg["status"] for leg in s["legs"]]
    assert s["phase"] == "done" and s["counts"]["active"] == 0 and s["counts"]["pending"] == 0
    assert s["counts"]["done"] + s["counts"]["failed"] == 12 and s["counts"]["done"] > 0
    assert any(t["attempts"] > 1 for t in s["tasks"])


def test_abort_returns_tasks_to_pending(monkeypatch):
    monkeypatch.setattr(config, "SIM_SPEEDUP", 20.0)
    q = _run_queue(EnergyModel(), 8, seed=9, abort_after_s=1.0)
    s = q.snapshot()
    assert s["phase"] == "aborted"
    assert s["counts"]["active"] == 0 and s["counts"]["pending"] > 0
    assert s["legs"][0]["status"] == "aborted"
    assert not q.link.flying and q.link.alt == 0


def test_queue_api(monkeypatch):
    monkeypatch.setattr(config, "SIM_SPEEDUP", 400.0)
    monkeypatch.setattr(appmain.tasks, "model", dataclasses.replace(appmain.tasks.model, charge_speedup=1e6))
    with TestClient(appmain.app) as c:
        s = c.post("/api/queue/divots", json={"count": 6, "holes": [1, 2], "seed": 4}).json()
        assert len(s["tasks"]) == 6 and s["legs"] and s["legs"][0]["path"]
        assert s["energy"]["pack_mah"] == 5000 and s["energy"]["calibrated"] is False
        assert c.post("/api/queue/run").status_code == 200
        assert c.post("/api/fly").status_code == 409  # the queue owns the drone
        deadline = time.time() + 30
        while time.time() < deadline:
            s = c.get("/api/queue").json()
            if not s["running"]:
                break
            time.sleep(0.1)
        assert s["phase"] == "done" and s["counts"]["done"] == 6, s["message"]
        assert c.post("/api/queue/clear").json()["tasks"] == []
