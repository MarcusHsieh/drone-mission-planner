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


DASHBOARD_KEYS = {"battery", "gps", "ekf", "vibration", "rangefinder", "sensors", "prearm", "link", "nav", "messages", "climb_ms", "roll_deg", "pitch_deg"}


def test_telemetry_has_dashboard_fields(monkeypatch):
    monkeypatch.setattr(config, "SIM_SPEEDUP", 200.0)
    with TestClient(app) as c:
        t = c.get("/api/telemetry").json()
        assert DASHBOARD_KEYS <= set(t)
        assert t["battery"]["capacity_mah"] > 0 and t["battery"]["voltage_v"] > 20  # 6S
        assert t["ekf"]["level"] == "ok" and t["prearm"]["ok"] is True
        c.post("/api/selection", json={"op": "set", "box": [-50, -30, -46, -26]})
        c.post("/api/preview", json={})
        assert c.post("/api/fly").status_code == 200
        with c.websocket_connect("/ws/telemetry") as ws:
            deadline = time.time() + 30
            while time.time() < deadline:
                t = ws.receive_json()
                if t["mode"] == "DOCKED" and t["distance_flown_m"] > 0:
                    break
        assert t["battery"]["consumed_mah"] > 0
        log = c.get("/api/messages").json()["messages"]
        texts = [m["text"] for m in log]
        assert "Armed" in texts and "Disarmed" in texts and "Mode AUTO" in texts
        ids = [m["id"] for m in log]
        assert ids == sorted(ids) and len(set(ids)) == len(ids)
