"""Fly a divot task queue on ArduPilot SITL, in legs with charge stops.
Skipped unless SITL is up (see test_sitl_flight.py):

    SITL_TEST=1 uv run pytest tests/test_sitl_queue.py -s

With SPEEDUP=10 it takes a few minutes.
"""

import argparse
import asyncio
import os

import pytest

from app import config

pytestmark = pytest.mark.skipif(not os.getenv("SITL_TEST"), reason="set SITL_TEST=1 with ArduPilot SITL running")


def test_divot_queue_with_charge_stops_on_sitl():
    from tools.fly_queue import run

    args = argparse.Namespace(divots=14, holes=None, seed=1, service_s=10.0, charge_speedup=10000.0, connection=config.SITL_CONNECTION, timeout=1500)
    r = asyncio.run(run(args))
    print(r)
    assert r["ok"], r["message"]
    assert len(r["legs"]) >= 2, "expected at least one charge stop"
    assert r["counts"]["done"] == 14
