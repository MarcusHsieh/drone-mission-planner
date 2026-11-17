"""Fly a real planner mission on ArduPilot SITL. Skipped unless SITL is up.

Start SITL first (see docs/concepts/sitl.md), then:

    SITL_TEST=1 uv run pytest tests/test_sitl_flight.py -s

SITL_CONNECTION picks the port (default tcp:127.0.0.1:5760). Running SITL
with SPEEDUP=10 makes this take about a minute.
"""

import os

import pytest

from app import config

pytestmark = pytest.mark.skipif(not os.getenv("SITL_TEST"), reason="set SITL_TEST=1 with ArduPilot SITL running")


def test_fly_hole_2_fairway_on_sitl():
    from tools.fly_sitl import fly

    r = fly(hole=2, feature="fairway", connection=config.SITL_CONNECTION, timeout_s=900, quiet=False)
    print(r)
    assert r["phase"] == "DONE", r["message"]
    assert r["missed"] == [] and r["reached_in_order"]
    assert r["landed_from_dock_m"] <= 3.0 and not r["armed"]
    # The track stays near the planned lines. Copter rounds corners a little.
    assert r["max_deviation_m"] < 8.0
    assert r["flown_m"] >= 0.9 * r["route_m"]
