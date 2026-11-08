"""HTTP and WebSocket API for the demo.

Run:  uv run uvicorn app.main:app --reload --port 8000
If frontend/dist exists (npm run build), it is served at / too.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import anthropic
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from . import config
from .agent import Agent
from .course import DOCK_LAT, DOCK_LON, DOCK_OSM_WAY, Course
from .geo import CELL_M
from .sim import DroneSim
from .state import MissionState
from .w3w import W3WCache

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

course = Course()
state = MissionState(course)
agent = Agent(course, state)
w3w = W3WCache(course)
sim = DroneSim(course.frame)

app = FastAPI(title="Drone Mission Planner (M1 demo)")


class SelectionEdit(BaseModel):
    op: str = Field(pattern="^(add|remove|toggle|set|clear)$")
    cells: list[tuple[int, int]] = []
    box: tuple[int, int, int, int] | None = None  # i0, j0, i1, j1 inclusive


class PreviewReq(BaseModel):
    group_ids: list[int] | None = None


class ChatReq(BaseModel):
    message: str = Field(min_length=1, max_length=2000)


@app.get("/api/course")
def get_course() -> dict:
    return {
        "name": course.name,
        "osm_relation": course.osm_relation,
        "dock": {"lat": DOCK_LAT, "lon": DOCK_LON, "osm_way": DOCK_OSM_WAY, "label": "Dock (hole 1 back tee)"},
        "frame": {"lat0": course.frame.lat0, "lon0": course.frame.lon0, "cell_m": CELL_M},
        "cell_bounds": course.cell_bounds,
        "holes": course.hole_summary(),
        "features": course.geojson(),
        "agent": {"model": config.AGENT_MODEL, "configured": bool(config.ANTHROPIC_API_KEY)},
        "w3w_configured": bool(config.W3W_API_KEY),
    }


@app.get("/api/state")
def get_state() -> dict:
    return state.snapshot()


@app.post("/api/selection")
def edit_selection(req: SelectionEdit) -> dict:
    if req.op == "clear":
        state.clear()
        return state.snapshot()
    cells = {tuple(c) for c in req.cells}
    if req.box:
        i0, j0, i1, j1 = req.box
        if (abs(i1 - i0) + 1) * (abs(j1 - j0) + 1) > 60000:
            raise HTTPException(400, "Box too large")
        cells |= {(i, j) for i in range(min(i0, i1), max(i0, i1) + 1) for j in range(min(j0, j1), max(j0, j1) + 1)}
    state.edit(req.op, cells)
    return state.snapshot()


@app.post("/api/preview")
def preview(req: PreviewReq) -> dict:
    state.preview(req.group_ids)
    return state.snapshot()


@app.post("/api/preview/clear")
def preview_clear() -> dict:
    state.clear_previews()
    return state.snapshot()


@app.post("/api/agent")
async def chat(req: ChatReq) -> dict:
    if not config.ANTHROPIC_API_KEY:
        raise HTTPException(503, "The agent needs ANTHROPIC_API_KEY in backend/.env. Restart the backend after adding it.")
    try:
        out = await run_in_threadpool(agent.chat, req.message)
    except anthropic.AuthenticationError:
        raise HTTPException(502, "Claude rejected the API key. Check ANTHROPIC_API_KEY in backend/.env.")
    except anthropic.NotFoundError:
        raise HTTPException(502, f"Model {config.AGENT_MODEL!r} was not found. Check AGENT_MODEL in backend/.env.")
    except anthropic.RateLimitError:
        raise HTTPException(429, "Claude rate limit or spend limit hit. Wait a moment and try again.")
    except anthropic.APIStatusError as e:
        raise HTTPException(502, f"Claude API error {e.status_code}: {e.message}")
    except anthropic.APIConnectionError:
        raise HTTPException(502, "Could not reach the Claude API. Check the network.")
    out["state"] = state.snapshot()
    return out


@app.post("/api/agent/reset")
def chat_reset() -> dict:
    agent.reset()
    return {"ok": True}


@app.get("/api/w3w")
async def w3w_lookup(i: int, j: int) -> dict:
    if not course.in_bounds((i, j)):
        raise HTTPException(400, "Cell is outside the course grid")
    return await run_in_threadpool(w3w.lookup, (i, j))


@app.post("/api/fly")
async def fly() -> dict:
    route = state.route()
    if not route:
        raise HTTPException(400, "Preview a path first.")
    try:
        sim.start(route["waypoints"])
    except RuntimeError as e:
        raise HTTPException(409, str(e))
    return {"ok": True, "route_m": round(route["total_m"])}


@app.post("/api/abort")
async def abort() -> dict:
    if not sim.flying:
        raise HTTPException(409, "Not flying")
    sim.abort()
    return {"ok": True}


@app.get("/api/telemetry")
def telemetry() -> dict:
    return sim.telemetry()


@app.websocket("/ws/telemetry")
async def ws_telemetry(ws: WebSocket) -> None:
    await ws.accept()
    q: asyncio.Queue = asyncio.Queue(maxsize=10)
    sim.subscribers.add(q)
    try:
        await ws.send_json(sim.telemetry())
        while True:
            await ws.send_json(await q.get())
    except WebSocketDisconnect:
        pass
    finally:
        sim.subscribers.discard(q)


# Serve the built frontend for a one-command demo.
DIST = Path(__file__).resolve().parents[2] / "frontend" / "dist"
if DIST.exists():
    app.mount("/assets", StaticFiles(directory=DIST / "assets"), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str):
        f = (DIST / path).resolve()
        ok = path and f.is_file() and f.is_relative_to(DIST)
        return FileResponse(f if ok else DIST / "index.html")
