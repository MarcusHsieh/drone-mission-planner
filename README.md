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
- **Dashboard**: every part of the app is a panel you can drag by its tab,
  stack as tabs, resize, float, close and reopen (**Panels** in the top
  bar). In-flight instruments like a ground station's: attitude indicator,
  battery (V, A, mAh used, time left), GPS (fix, satellites, HDOP), health
  (arming checks, EKF, vibration, sensors), autopilot messages, link and the
  downward rangefinder. The layout is saved in your browser; **Reset layout**
  puts it back. QGroundControl stays the tool for setup and calibration. See
  [Flight dashboard](docs/concepts/dashboard.md).
- **Agent**: Claude (Haiku by default) with tool calls on top of the planner.
  "Survey the fairway on hole 4" pans the map, selects the fairway, fills in
  the stats and draws the path preview.
- **Task queue with charge stops**: generate fake golf divots as tasks
  (pending, active, done, failed). The planner splits the queue into legs
  that each fit on one battery by a conservative, configurable energy model
  (placeholders for a 4S 5000 mAh X500-class quad, not calibrated), and the
  drone flies each leg, lands on the dock, charges (simulated) and flies the
  next. The Tasks panel shows every leg's prediction next to what the vehicle
  measured. See [Task queue, legs and charge stops](docs/concepts/task-queue.md).
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
| `ENERGY_PACK_MAH`, `ENERGY_RESERVE_PCT`, `ENERGY_MARGIN`, `ENERGY_HOVER_A`, ... | `5000`, `30`, `1.15`, `20`, ... | The task queue's energy model. All placeholders; see [Task queue settings](docs/concepts/task-queue.md#settings-backendenv). |
| `CHARGE_C`, `CHARGE_SPEEDUP` | `1.0`, `60` | Charge rate, and how much faster than real time simulated charging runs. |

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
6. Open the **Tasks** tab, click **Generate** (20 divots), then **Run queue**.
   The divots and the planned legs show on the map; the drone flies a leg,
   lands, charges and flies the next, and the divots turn green as they are
   done.

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
   the GCS after Fly uploads it). The planner's own panels cover in-flight
   monitoring; use the GCS for parameters, calibration and setup.

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
uv run python -m tools.fly_queue --divots 20 --charge-speedup 600   # a divot task queue in legs with charge stops
SITL_TEST=1 uv run pytest tests/test_sitl_flight.py -s
```

CI (`.github/workflows/ci.yml`) does the same on every PR and push to
`main`: the backend tests, then hole 2 flown on SITL at 10x speed and a
14-divot task queue flown in legs with charge stops. The SITL
image is kept in the GitHub Actions cache, so it only recompiles when
`sitl/` changes.

## Numbers are estimates

Pass spacing assumes 15 m altitude, an 80 degree camera field of view and 70%
sidelap (about 7.6 m between passes), flown at 5 m/s with 3 s per turn. The
simulated battery follows the task queue's energy model: a 4S 5000 mAh
pack drawing a fixed, uncalibrated current per flight phase. All of these are
placeholders until the camera is chosen and the airframe's battery use is
measured.

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
  app/tasks.py     tasks and fake divots;  app/energy.py  the battery energy model
  app/charge_plan.py  split a task queue into one-battery legs with charge stops
  app/vehicle.py   the planner/vehicle boundary (LegMission, VehicleLink)
  app/task_queue.py   fly the legs, update task status, charge between legs
  app/vitals.py    message log and health checks shared by the sim and SITL
  app/mavlink_mission.py  route -> ArduPilot mission items, MAVLink mission upload
  app/sitl_drone.py       flies the route on ArduPilot SITL (FLIGHT_BACKEND=sitl)
  tools/fly_sitl.py       headless SITL flight with a pass/fail report
  tools/fly_queue.py      headless SITL task queue with charge stops
  tests/           pytest (the agent test uses a scripted client, no API calls)
frontend/  Vite + TypeScript + MapLibre GL (+ three.js for the 3D view, src/view3d.ts)
  src/layout.ts    dashboard panels (dockview): default layout, save/restore, Panels menu
  src/panels.ts    instrument panels and message log; src/hud.ts the attitude indicator
  src/tasks.ts     the Tasks panel and the divot and leg map layers
sitl/      ArduPilot SITL in Docker (Dockerfile, compose.yaml, start script, parameters)
```

Run the tests with `cd backend && uv run pytest`.

## Docs

Concept explanations (the grid, coverage path planning and boustrophedon
decomposition, course data, the agent) and a design decision log are in
[`docs/`](docs/README.md).

## Not in M1

Brush selection, the agent's `select_box` tool, charge stops for survey
routes (the task queue has them; the Fly button doesn't), precision landing
and real charging on the dock, saved missions, and Docker for the planner
itself. These are planned
for M2 and M3.
