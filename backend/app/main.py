"""HTTP and WebSocket API for the demo.

Run:  uv run uvicorn app.main:app --reload --port 8000
If frontend/dist exists (npm run build), it is served at / too.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

import anthropic
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from . import config
from .agent import Agent
from .buildings import Buildings
from .course import DOCK_LAT, DOCK_LON, DOCK_OSM_WAY, Course
from .geo import CELL_M
from .sim import DroneSim
from .sitl_drone import SitlDrone
from .state import MissionState
from .scan import DivotField
from .task_queue import TaskQueue
from .w3w import W3WCache

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

course = Course()
state = MissionState(course)
w3w = W3WCache(course)
buildings = Buildings(course)
if config.FLIGHT_BACKEND == "sitl":
    sim: DroneSim | SitlDrone = SitlDrone(course.frame, config.SITL_CONNECTION)
else:
    sim = DroneSim(course.frame)
# The task queue plans legs and drives the same vehicle link the Fly button uses.
tasks = TaskQueue(course, sim)
# Divots you place (hidden from the planner) and the simulated camera that finds them.
field = DivotField(course, tasks, sim)
agent = Agent(course, state, tasks, field)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    await sim.on_startup()
    yield


app = FastAPI(title="Drone Mission Planner (M1 demo)", lifespan=lifespan)


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
        # Sent to the browser on purpose: the 3D tiles are fetched from there.
        "google_3d_key": config.GOOGLE_MAPS_API_KEY or None,
        "flight_backend": config.FLIGHT_BACKEND,
    }


@app.get("/api/buildings")
def get_buildings() -> dict:
    return buildings.get()


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


class DivotReq(BaseModel):
    count: int = Field(default=20, ge=1, le=200)
    holes: list[int] | None = None
    seed: int | None = None
    replace: bool = True
    service_s: float = Field(default=15.0, ge=0, le=600)


def queue_snapshot() -> dict:
    return {**tasks.snapshot(), "field": field.snapshot()}


@app.get("/api/queue")
def queue_state() -> dict:
    return queue_snapshot()


class PlacedReq(BaseModel):
    op: str = Field(pattern="^(add|remove|clear|scatter)$")
    points: list[tuple[float, float]] = []  # lon, lat
    ids: list[str] = []
    count: int = Field(default=10, ge=1, le=200)
    holes: list[int] | None = None
    seed: int | None = None


@app.post("/api/field/divots")
def field_divots(req: PlacedReq) -> dict:
    if field.scanning:
        raise HTTPException(409, "A scan is flying. Wait for it to land.")
    if req.op == "add":
        field.add([course.frame.to_xy(lon, lat) for lon, lat in req.points])
    elif req.op == "remove":
        field.remove(req.ids)
    elif req.op == "clear":
        field.clear()
    else:
        try:
            field.scatter(req.count, req.holes, req.seed)
        except ValueError as e:
            raise HTTPException(400, str(e))
    return queue_snapshot()


@app.post("/api/scan")
async def scan() -> dict:
    route = state.route()
    if not route:
        raise HTTPException(400, "Preview a path over the area to scan first.")
    try:
        field.start_scan(route["waypoints"])
    except RuntimeError as e:
        raise HTTPException(409, str(e))
    return queue_snapshot()


@app.post("/api/queue/divots")
def queue_divots(req: DivotReq) -> dict:
    try:
        tasks.generate(req.count, req.holes, req.seed, req.replace, req.service_s)
    except (RuntimeError, ValueError) as e:
        raise HTTPException(409, str(e))
    return queue_snapshot()


@app.post("/api/queue/clear")
def queue_clear() -> dict:
    try:
        tasks.clear()
    except RuntimeError as e:
        raise HTTPException(409, str(e))
    return queue_snapshot()


@app.post("/api/queue/run")
async def queue_run() -> dict:
    if field.scanning:
        raise HTTPException(409, "A scan is flying. Run the queue after it lands.")
    try:
        tasks.start()
    except RuntimeError as e:
        raise HTTPException(409, str(e))
    return queue_snapshot()


@app.post("/api/queue/abort")
async def queue_abort() -> dict:
    try:
        tasks.abort()
    except RuntimeError as e:
        raise HTTPException(409, str(e))
    return queue_snapshot()


@app.post("/api/fly")
async def fly() -> dict:
    if tasks.running:
        raise HTTPException(409, "The task queue is running. Abort it first.")
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


@app.get("/api/messages")
def messages() -> dict:
    """The full message log (STATUSTEXT and planner events). The WebSocket only carries the last few."""
    return {"messages": sim.msglog.all()}


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
