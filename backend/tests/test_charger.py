"""Passive charging on the dock: no button starts it, flights only wait when short."""

import asyncio
import dataclasses

from app.charger import DockCharger, need_pct
from app.course import Course
from app.energy import EnergyModel
from app.sim import DroneSim


def _setup(speedup: float):
    model = dataclasses.replace(EnergyModel.from_env(), charge_speedup=speedup)
    sim = DroneSim(Course().frame, energy=model)
    return model, sim, DockCharger(sim, model)


def test_charges_on_the_dock_by_itself():
    async def go():
        model, sim, ch = _setup(speedup=20000)  # a full charge in well under a second
        sim.consumed_mah = model.pack_mah * 0.6  # landed at 40%
        ch.ensure_running()
        for _ in range(100):
            await asyncio.sleep(0.1)
            if sim.battery >= 99.9:
                break
        await ch.stop()
        return sim.battery, ch.state

    pct, state = asyncio.run(go())
    assert pct >= 99.9 and state == "full"


def test_charge_rate_follows_the_model():
    model, _sim, ch = _setup(speedup=1)
    # 1C with a 1.15 taper: 0% -> 100% in 1.15 h, plus the handling time before it starts.
    assert abs(100 / ch.pct_per_wall_s() - 1.15 * 3600) < 1
    assert ch.settle_wall_s() == model.charge_handling_s


def test_flight_waits_only_when_short():
    model = EnergyModel.from_env()
    # A 1000 mAh flight needs 20% plus the 30% reserve.
    assert need_pct(model, 1000) == 50.0
    assert need_pct(model, model.usable_mah) == 100.0

    async def go():
        model, sim, ch = _setup(speedup=20000)
        sim.consumed_mah = model.pack_mah * 0.4  # 60%: enough for a 1000 mAh flight already
        t = asyncio.get_running_loop().time()
        assert await ch.wait_for(need_pct(model, 1000), "a test flight")
        quick = asyncio.get_running_loop().time() - t
        assert await ch.wait_for(need_pct(model, model.usable_mah), "a full leg")
        await ch.stop()
        return quick, sim.battery

    quick, pct = asyncio.run(go())
    assert quick < 0.05 and pct >= 99
