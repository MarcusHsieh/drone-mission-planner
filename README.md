# Drone Mission Planner (M1 demo)

A web app for planning drone survey flights over a golf course. M1 covers the
front nine of **San Ramon Golf Club** (OSM relation
[3566976](https://www.openstreetmap.org/relation/3566976)), with the dock at
the hole 1 back tee (37.733897, -121.929056).

What it does:

- Satellite map with the OSM fairways, greens, tees, bunkers and hole lines.
- A **3 m grid anchored at the dock** (cell `(i, j)` = `3i..3i+3` m east and
  `3j..3j+3` m north of the dock).
- Select cells by hand: click to toggle, **Shift+drag** to add a box,
  **Alt+drag** to remove a box. Right-click a cell for its coordinates and
  its what3words address (one cached API call per cell).
- Selected cells are grouped by **4-connectivity** (cells that share an edge).
  Group numbers stay stable when you edit other groups. Each group shows
  cells, area, size, holes touched and distance from the dock.
- **Path preview**: coverage path per group. Curved and dogleg fairways are
  split into pieces (boustrophedon decomposition) so passes don't fly over
  the rough, the sweep angle is searched, and pieces and groups are ordered
  (nearest neighbor plus 2-opt) for a short route from the dock and back,
  with length and time estimates.
- **Agent**: Claude (Haiku by default) with tool calls on top of the planner.
  "Survey the fairway on hole 4" pans the map, selects the fairway, fills in
  the stats and draws the path preview.
- **Simulated drone**: Fly takes off from the dock, flies the preview, returns
  and lands, with live telemetry (mode, battery, altitude, speed, heading, GPS)
  over a WebSocket. Abort returns to the dock. ArduPilot SITL replaces this
  in M3.

## Run it

You need [uv](https://docs.astral.sh/uv/) and Node 20+.

```bash
# 1. Backend config (once)
cp backend/.env.example backend/.env     # PowerShell: Copy-Item backend\.env.example backend\.env
# then put your key in backend/.env:  ANTHROPIC_API_KEY=sk-ant-...

# 2. Build the frontend (once, and after frontend changes)
cd frontend && npm install && npm run build && cd ..

# 3. Start everything on one port
cd backend && uv run uvicorn app.main:app --port 8000
```

Open http://localhost:8000.

For frontend development with hot reload, run the backend as above and
`npm run dev` in `frontend/`, then open http://localhost:5173.

### Settings (`backend/.env`)

| Variable | Default | Notes |
| --- | --- | --- |
| `ANTHROPIC_API_KEY` | (none) | Needed for the agent only. Manual selection works without it. |
| `AGENT_MODEL` | `claude-haiku-5-5` | The one place the model is set. Try `claude-sonnet-5-5` if Haiku picks the wrong tool. |
| `AGENT_EFFORT` | `medium` | `low` is faster and cheaper, `high` is more careful. |
| `W3W_API_KEY` | (none) | Optional. Without it the cell popup says what3words is not configured. |
| `SIM_SPEEDUP` | `5` | Simulated flight speed relative to real time. |

The agent's token use for each reply is shown under the reply and logged by
the backend.

## Demo script

1. Open the app. The course, dock and hole numbers are on the map.
2. Click **Survey the fairway on hole 4** (or type it). The map pans to hole
   4, the fairway cells light up as **two groups** (OSM maps the fairway and
   the approach to the green as separate shapes with a gap between them),
   the stats fill in and the passes are drawn.
3. Ask **"Add hole 5's fairway. What's the total area?"** to show it keeping
   track of groups.
4. Click a few cells or shift-drag a box to show manual editing. Groups
   update and keep their numbers.
5. Click **Preview path**, then **Fly**. Watch the simulated drone fly the
   route and land on the dock.

## Numbers are estimates

Pass spacing assumes 15 m altitude, an 80 degree camera field of view and 70%
sidelap (about 7.6 m between passes), flown at 5 m/s with 3 s per turn. The
simulated battery is a straight line from 100% to 0% over 20 minutes of
flight. All of these are placeholders until the camera, airframe and battery
are chosen.

## Course data

`backend/app/data/osm_front9.json` is a snapshot of the OSM features near
holes 1-9, read from the OSM API on 2026-11-08. OSM fairways have no hole
number, so each fairway is assigned to the hole whose center line runs
through it the most. When a fairway is turned into cells, pieces under 10
cells (slivers around the greens) are dropped. The outlines cite old Yahoo
imagery, so check them against the satellite layer and retrace a hole in OSM
if it is off.

## Layout

```
backend/   FastAPI app: course model, grid, grouping, planner, agent, sim, w3w cache
  app/course.py    OSM snapshot -> holes, fairways, greens, tees (local meters)
  app/geo.py       dock-centered local frame and 3 m cells (mirrored in frontend/src/geo.ts)
  app/planner.py   4-connected groups with stable ids, coverage path planning
  app/state.py     the mission draft: selection, groups, previews, route
  app/agent.py     Claude tool-use loop and tools
  app/sim.py       simulated drone and telemetry
  tests/           pytest (the agent test uses a scripted client, no API calls)
frontend/  Vite + TypeScript + MapLibre GL
```

Run the tests with `cd backend && uv run pytest`.

## Not in M1

Brush selection, the agent's `select_box` tool, sorties and battery-based
charge stops, ArduPilot SITL, saved missions, and Docker. These are planned
for M2 and M3.
