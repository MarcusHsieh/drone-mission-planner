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
- **3D view**: 2D / 3D / Chase buttons tilt the map and show the drone at its
  real altitude and attitude, the planned passes at survey altitude and the
  flown track in 3D, over OSM building boxes or, with a Google key, Google's
  photorealistic 3D buildings and trees.
- **Agent**: Claude (Haiku by default) with tool calls on top of the planner.
  "Survey the fairway on hole 4" pans the map, selects the fairway, fills in
  the stats and draws the path preview.
- **Simulated drone**: Fly takes off from the dock, flies the preview, returns
  and lands, with live telemetry (mode, battery, altitude, speed, heading, GPS)
  over a WebSocket. Abort returns to the dock.
- **ArduPilot SITL** (optional): with `FLIGHT_BACKEND=sitl`, Fly uploads the
  route as a MAVLink mission to ArduPilot SITL (a generic ArduCopter quad)
  and the map shows the autopilot's real track. See
  [Fly on ArduPilot SITL](#fly-on-ardupilot-sitl).

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

**Or do steps 2 and 3 with one command** from the repo root (step 1 is still
needed once for the agent):

```bash
.\run            # Windows (PowerShell or cmd)
./run.sh         # macOS / Linux / Git Bash
```

It installs frontend packages on the first run and again whenever
`frontend/package-lock.json` changes (after a pull that adds a package),
rebuilds the frontend only when its files changed, and serves everything at http://localhost:8000. Add
`--dev` (`.\run --dev`) to run the backend and the Vite dev server together
with hot reload at http://localhost:5173. Ctrl+C stops everything.

For frontend development with hot reload, run the backend as above and
`npm run dev` in `frontend/`, then open http://localhost:5173.

### Settings (`backend/.env`)

| Variable | Default | Notes |
| --- | --- | --- |
| `ANTHROPIC_API_KEY` | (none) | Needed for the agent only. Manual selection works without it. |
| `AGENT_MODEL` | `claude-haiku-5-5` | The one place the model is set. Try `claude-sonnet-5-5` if Haiku picks the wrong tool. |
| `AGENT_EFFORT` | `medium` | `low` is faster and cheaper, `high` is more careful. |
| `W3W_API_KEY` | (none) | Optional. Without it the cell popup says what3words is not configured. |
| `GOOGLE_MAPS_API_KEY` | (none) | Optional. Google photorealistic 3D buildings and trees in the 3D view. Without it the 3D view shows OSM building boxes. See [3D buildings and trees](docs/concepts/sitl.md#3d-buildings-and-trees). |
| `SIM_SPEEDUP` | `5` | Simulated flight speed relative to real time (built-in sim only). |
| `FLIGHT_BACKEND` | `sim` | `sim` for the built-in sim, `sitl` to fly on ArduPilot SITL. |
| `SITL_CONNECTION` | `tcp:127.0.0.1:5760` | Where SITL's MAVLink port is (only with `FLIGHT_BACKEND=sitl`). |

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

## Fly on ArduPilot SITL

The planner can fly its route on ArduPilot SITL, the real ArduPilot flight
code running against a simulated quad. SITL runs headless in Docker, and you
watch the flight on the planner's map (and, if you like, in a ground control
station at the same time). How it works and its limits are in
[docs/concepts/sitl.md](docs/concepts/sitl.md).

### On Windows (the demo)

You need [Docker Desktop](https://www.docker.com/products/docker-desktop/)
running, plus uv and Node as above. Docker Desktop uses WSL2 underneath, but
you don't need to open or set up a Linux distro.

1. Start SITL from the repo root and leave it running:

   ```powershell
   docker compose -f sitl/compose.yaml up --build
   ```

   The first run downloads and compiles ArduPilot (about 10-20 minutes).
   Later runs start in a few seconds without `--build`. It is ready when the
   log shows `SERIAL0 on TCP port 5760`. Each start is a fresh quad sitting on
   the dock.

2. In `backend/.env`, add:

   ```
   FLIGHT_BACKEND=sitl
   ```

3. In a second terminal, start the planner as usual: `.\run`. Open
   http://localhost:8000. The Drone panel shows an **ArduPilot SITL** chip and
   "On dock, ready" once it is connected.

4. Select an area (or ask the agent), click **Preview path**, then **Fly**.
   The mission uploads, the autopilot arms (this can take up to a minute
   right after SITL starts, while its GPS and EKF settle), takes off, flies
   the passes and lands on the dock. The panel counts the waypoints the
   autopilot reports reached. **Abort** switches it to RTL.

   Click **3D** (top right of the map) to tilt the view and watch the quad
   fly at its real altitude over the course, or **Chase** to have the camera
   follow it. Right-drag or Ctrl+drag turns and tilts the camera; **2D**
   goes back to the planning view. See
   [3D view](docs/concepts/sitl.md#3d-view).

5. Optional, for a standard autopilot view next to the planner: install
   [QGroundControl](https://docs.qgroundcontrol.com/master/en/qgc-user-guide/getting_started/download_and_install.html)
   or [Mission Planner](https://ardupilot.org/planner/docs/mission-planner-installation.html)
   and connect it over **TCP to 127.0.0.1, port 5762**.
   - QGroundControl: Application Settings > Comm Links > Add, type TCP, host
     `127.0.0.1`, port `5762`, then Connect.
   - Mission Planner: top right, choose TCP, click Connect, enter `127.0.0.1`
     and `5762`.

   It shows the same vehicle, its mode and its mission (read the mission in
   the GCS after Fly uploads it).

To run the simulation five times faster than real time, set `SPEEDUP`
first: `$env:SPEEDUP=5; docker compose -f sitl/compose.yaml up` in
PowerShell (`SPEEDUP=5 docker compose ...` in bash). Ctrl+C stops SITL.

If Fly says it is not connected: check that the SITL container is running
and that nothing else is using port 5760.

### Headless (cloud, Linux, CI)

```bash
docker compose -f sitl/compose.yaml up --build -d     # or SPEEDUP=10 for quick runs
cd backend
uv run python -m tools.fly_sitl --hole 2              # flies hole 2's fairway, prints a JSON report
SITL_TEST=1 uv run pytest tests/test_sitl_flight.py -s
```

CI (`.github/workflows/ci.yml`) does the same on every PR and push to
`main`: the backend tests, then hole 2 flown on SITL at 10x speed. The SITL
image is kept in the GitHub Actions cache, so it only recompiles when
`sitl/` changes.

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
  app/mavlink_mission.py  route -> ArduPilot mission items, MAVLink mission upload
  app/sitl_drone.py       flies the route on ArduPilot SITL (FLIGHT_BACKEND=sitl)
  tools/fly_sitl.py       headless SITL flight with a pass/fail report
  tests/           pytest (the agent test uses a scripted client, no API calls)
frontend/  Vite + TypeScript + MapLibre GL (+ three.js for the 3D view, src/view3d.ts)
sitl/      ArduPilot SITL in Docker (Dockerfile, compose.yaml, start script, parameters)
```

Run the tests with `cd backend && uv run pytest`.

## Docs

Concept explanations (the grid, coverage path planning and boustrophedon
decomposition, course data, the agent) and a design decision log are in
[`docs/`](docs/README.md).

## Not in M1

Brush selection, the agent's `select_box` tool, sorties and battery-based
charge stops (including the autonomous recharge loop on SITL), saved missions,
and Docker for the planner itself. These are planned
for M2 and M3.
