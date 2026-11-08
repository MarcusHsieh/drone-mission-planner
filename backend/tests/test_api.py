import time

from fastapi.testclient import TestClient

from app import config
from app.main import app


def test_select_preview_fly_roundtrip(monkeypatch):
    monkeypatch.setattr(config, "SIM_SPEEDUP", 200.0)
    with TestClient(app) as c:
        course = c.get("/api/course").json()
        assert course["dock"]["lat"] == 37.733897
        assert len(course["holes"]) == 9
        s = c.post("/api/selection", json={"op": "set", "box": [-60, -40, -40, -20]}).json()
        assert s["total_cells"] == 21 * 21 and len(s["groups"]) == 1
        s = c.post("/api/selection", json={"op": "toggle", "cells": [[-50, -30]]}).json()
        assert s["total_cells"] == 21 * 21 - 1
        assert c.post("/api/fly").status_code == 400  # no preview yet
        s = c.post("/api/preview", json={}).json()
        assert s["route"]["total_m"] > 0 and s["groups"][0]["path"]
        assert c.post("/api/fly").status_code == 200
        with c.websocket_connect("/ws/telemetry") as ws:
            modes = set()
            deadline = time.time() + 30
            while time.time() < deadline:
                t = ws.receive_json()
                modes.add(t["mode"])
                if t["mode"] == "DOCKED" and "AUTO" in modes:
                    break
        assert {"TAKEOFF", "AUTO", "DOCKED"} <= modes
        assert t["distance_flown_m"] >= s["route"]["total_m"] - 5


def test_agent_without_key_is_clear(monkeypatch):
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "")
    with TestClient(app) as c:
        r = c.post("/api/agent", json={"message": "survey hole 4"})
        assert r.status_code == 503 and "ANTHROPIC_API_KEY" in r.json()["detail"]


def test_w3w_without_key():
    with TestClient(app) as c:
        r = c.get("/api/w3w", params={"i": 0, "j": 0}).json()
        assert r["words"] is None or isinstance(r["words"], str)
