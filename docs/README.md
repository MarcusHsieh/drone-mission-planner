# Project docs

These pages explain the ideas behind the drone mission planner and how this
codebase applies them. They are written for three kinds of reader: teammates
who need to change the code, advisors and graders who want to know why it
works the way it does, and whoever picks the project up later.

Each concept page has the same three parts, so you can stop reading when you
have what you need:

1. **In this project**: what the concept does here and where it is in the code.
2. **Background**: the general idea, with no knowledge of this codebase needed.
3. **Our choices and limits**: what we chose over the alternatives, and what
   doesn't work yet.

## Pages

| Page | What it covers |
| --- | --- |
| [Coordinate frame and grid](concepts/coordinate-frame-and-grid.md) | The dock-centered local frame, the 3 m cell grid, and how cells become groups |
| [Coverage path planning](concepts/coverage-planning.md) | Pass spacing, boustrophedon decomposition, sweep angle search, and tour ordering |
| [Course data](concepts/course-data.md) | The OpenStreetMap snapshot and how fairways, greens and tees are matched to holes |
| [Agent](concepts/agent.md) | How Claude drives the planner through tool calls, and what it is not allowed to do |
| [ArduPilot SITL and MAVLink missions](concepts/sitl.md) | How a route becomes an ArduPilot mission, how it is uploaded and flown on SITL, and how to run it headless or as a demo |
| [Flight dashboard](concepts/dashboard.md) | The movable panels, what each instrument shows and where it comes from in MAVLink, and what stays in QGroundControl |
| [Task queue, legs and charge stops](concepts/task-queue.md) | Fake divots as tasks, the energy model, splitting the queue into legs that each fit on a battery, charging on the dock, and the boundary between the planner and the vehicle |
| [Design decisions](decisions.md) | A short log of decisions that shaped the project, with the reasons and alternatives |

For setup, running the app and the demo script, see the [main README](../README.md).

## Glossary

| Term | Meaning here |
| --- | --- |
| **Dock** | The charging station at the hole 1 back tee. It is the origin (0, 0) of the local frame and where every flight starts and ends. |
| **Local frame** | Meters east (x) and north (y) of the dock. All planning happens in this frame, and lon/lat is used only at the edges (the OSM input and the map display). |
| **Cell** | A 3 m by 3 m square of the grid, named by its integer index `(i, j)`. Cell `(i, j)` covers `3i..3i+3` m east and `3j..3j+3` m north of the dock. |
| **Selection** | The set of cells the user (or the agent) has marked for survey. It is the source of truth that everything else is derived from. |
| **Group** | A 4-connected set of selected cells (cells that share an edge). Each group gets its own coverage path. Group numbers stay the same when you edit other groups. |
| **Pass** | One straight flight line across the survey area. Neighboring passes overlap so the camera images can be stitched. |
| **Sweep angle** | The direction of the passes within a group. The planner searches for the best one. |
| **Piece** | A part of a group that can be covered by back-and-forth passes without flying over ground outside the group. Classical papers call these "cells". We say "piece" so they aren't confused with grid cells. |
| **Footprint** | The width of ground one camera image covers across the flight direction. It depends on altitude and field of view. |
| **Sidelap** | How much neighboring passes' footprints overlap, as a fraction of the footprint. |
| **Preview** | The planned coverage path for one group, with its length, number of passes and estimated time. |
| **Route** | The full trip: dock, then every previewed group in the best order, then back to the dock. |
| **SITL** | Software in the loop: the real ArduPilot flight code running on a PC against a simulated vehicle. |
| **MAVLink** | The message protocol between the autopilot and ground software (the planner backend, QGroundControl, Mission Planner). |
| **Mission item** | One step of an ArduPilot mission: a command such as takeoff, waypoint or land, with its position and parameters. |
| **GCS** | Ground control station, such as QGroundControl or Mission Planner. |
| **Panel** | One movable part of the web app (the map, the agent, an instrument). Panels can be stacked as tabs, resized, floated and closed. |
| **STATUSTEXT** | A text message from the autopilot with a severity, such as `PreArm: GPS not healthy`. Shown in the Messages panel. |
| **EKF** | ArduPilot's extended Kalman filter: it fuses IMU, GPS, compass and barometer into the position and attitude the autopilot flies on. |
| **HDOP** | Horizontal dilution of precision: how good the GPS satellites' geometry is for a position fix. Lower is better. |
| **Pre-arm checks** | ArduPilot's checks before it allows arming (sensors, calibration, GPS, EKF, battery, RC). |
| **Sortie** | One flight on one battery charge. Survey routes from the Fly button are still assumed to fit on one battery; the task queue splits its work into sorties, called legs there. |
| **Task** | A spot the drone flies to and works at (descend, hold, climb back), with a status: pending, active, done or failed. In simulation the tasks are fake divots. |
| **Divot** | A patch of turf torn out by a golf shot. Here, a fake one: a random point inside a fairway, used as a task. |
| **Leg** | One flight of the task queue: take off from the dock with a full pack, visit some tasks, land on the dock with at least the reserve left. |
| **Charge stop** | Time on the dock between two legs while the pack charges (simulated). |
| **Energy model** | The planner's prediction of current draw per flight phase (hover, cruise, climb, descent), with a reserve and a safety margin. Placeholders until calibrated on the real airframe. |
| **Reserve** | The share of the pack a leg must still have when it lands, by the model. |
| **Vehicle link** | The code that flies one leg and reports progress: the built-in sim, or ArduPilot over MAVLink (SITL now, a real Pixhawk later). |

## Keeping these docs current

The docs live in the repo so they are changed in the same pull request as the
code they describe. The PR template asks whether the docs need an update.
To keep pages from drifting:

- Link to the code by file and function name, not by line number.
- Don't copy constants. Say where a number comes from (for example
  `planner.ALTITUDE_M`). Quote a value only to explain it, and mark it as
  current as of a date or milestone.
- When a limit listed under "Our choices and limits" gets fixed, update that
  section in the same PR.

### What to update when the code changes

Any change to behavior, an algorithm, a number, a tool or an API comes with
its docs update in the same PR. This applies to people and to AI coding
assistants alike.

| If you change... | Update |
| --- | --- |
| `backend/app/geo.py`, `frontend/src/geo.ts`, grouping in `planner.py` or `state.py` | [Coordinate frame and grid](concepts/coordinate-frame-and-grid.md) |
| Coverage planning in `planner.py` (spacing, decomposition, angle search, `plan_tour`), the route in `state.py` | [Coverage path planning](concepts/coverage-planning.md) |
| `backend/app/course.py`, `backend/app/data/` | [Course data](concepts/course-data.md) |
| `backend/app/agent.py` (tools, system prompt, loop), agent settings in `config.py` | [Agent](concepts/agent.md) |
| `backend/app/mavlink_mission.py`, `backend/app/sitl_drone.py`, `backend/tools/fly_sitl.py`, `sitl/`, `.github/workflows/ci.yml`, SITL settings in `config.py` | [ArduPilot SITL and MAVLink missions](concepts/sitl.md) |
| `frontend/src/layout.ts`, `panels.ts`, `hud.ts`, panel contents in `frontend/index.html`, `backend/app/vitals.py`, telemetry fields in `sim.py` or `sitl_drone.py` | [Flight dashboard](concepts/dashboard.md) |
| `backend/app/tasks.py`, `energy.py`, `charge_plan.py`, `vehicle.py`, `task_queue.py`, `start_leg`/`leg_progress`/`recharge` in `sim.py` or `sitl_drone.py`, `build_leg_mission`, `backend/tools/fly_queue.py`, `frontend/src/tasks.ts` | [Task queue, legs and charge stops](concepts/task-queue.md) |
| A new concept with no page yet (for example sorties, charging, SITL) | A new page in `concepts/` with the same three sections, linked from the table above |
| A choice between real alternatives that someone would ask about later | A new entry in [decisions](decisions.md) (add new entries, never rewrite old ones) |
| New terms | The glossary above |
| Setup, settings or how to run the app | The [main README](../README.md) |

If a change truly needs no docs update, check "No docs change needed" in the
PR template and say why in one line.
