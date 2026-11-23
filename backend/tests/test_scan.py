import dataclasses
import time

from fastapi.testclient import TestClient
from shapely.geometry import LineString, Point

from app import config, scan
from app import main as appmain


def _wait(c, pred, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        s = c.get("/api/queue").json()
        if pred(s):
            return s
        time.sleep(0.1)
    raise AssertionError(f"timed out: {s['message']}")


def test_footprint_matches_pass_spacing():
    # Passes are spaced at footprint * (1 - sidelap), so a scan sees every point of the area.
    from app import planner

    assert abs(scan.footprint_m(planner.ALTITUDE_M) * (1 - planner.SIDELAP) - planner.pass_spacing_m()) < 1e-9


def test_place_scan_then_repair(monkeypatch):
    monkeypatch.setattr(config, "SIM_SPEEDUP", 60.0)
    monkeypatch.setattr(appmain.tasks, "model", dataclasses.replace(appmain.tasks.model, charge_speedup=1e6))
    with TestClient(appmain.app) as c:
        c.post("/api/queue/clear")
        c.post("/api/field/divots", json={"op": "clear"})
        c.post("/api/selection", json={"op": "clear"})  # other tests share the app's state
        s = c.post("/api/field/divots", json={"op": "scatter", "count": 5, "holes": [1], "seed": 3}).json()
        # One placed by hand far off any fairway we scan: hole 9's tee area.
        h9 = appmain.course.holes[9].line.coords[0]
        lon, lat = appmain.course.frame.to_lonlat(*h9)
        s = c.post("/api/field/divots", json={"op": "add", "points": [[lon, lat]]}).json()
        assert len(s["field"]["placed"]) == 6 and s["tasks"] == []  # hidden: no tasks yet
        assert c.post("/api/scan").status_code == 400  # no route yet
        cells = appmain.course.feature_cells(1, "fairway")
        c.post("/api/selection", json={"op": "set", "cells": [list(x) for x in cells]})
        c.post("/api/preview", json={})
        assert c.post("/api/scan").status_code == 200
        assert c.post("/api/queue/run").status_code == 409  # scan flying
        s = _wait(c, lambda s: not s["field"]["scanning"] and s["field"]["last_scan"])
        f = s["field"]
        # Every divot under the camera is found (no simulated misses); hole 9's is never under the camera.
        assert f["last_scan"]["not_covered"] >= 1
        assert all(d["found_task"] for d in f["placed"] if d["seen"])
        found = [d for d in f["placed"] if d["found_task"]]
        assert len(found) == len(s["tasks"]) >= 4
        assert all(d["found_error_m"] < 3 for d in found)
        assert all(t["status"] == "pending" for t in s["tasks"]) and s["legs"]
        assert c.post("/api/queue/run").status_code == 200
        s = _wait(c, lambda s: not s["running"], timeout=60)
        assert s["phase"] == "done" and s["counts"]["done"] == len(s["tasks"])
        c.post("/api/field/divots", json={"op": "clear"})
        c.post("/api/queue/clear")
        c.post("/api/selection", json={"op": "clear"})


def test_fairway_scan_covers_every_piece_on_every_hole():
    # Collars around greens and other scraps are scanned too: no part of any
    # hole's fairway is outside the camera footprint along the planned route.
    from app import planner
    from app.state import MissionState

    course = appmain.course
    half = scan.footprint_m(planner.ALTITUDE_M) / 2
    for h, hole in course.holes.items():
        if hole.fairway is None:
            continue
        s = MissionState(course)
        s.edit("add", course.feature_cells(h, "fairway"))
        s.preview()
        seen = LineString(s.route()["waypoints"]).buffer(half)
        assert hole.fairway.difference(seen).area < 1.0, f"hole {h}"
