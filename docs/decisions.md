# Design decisions

A short log of decisions that shaped the project. Each entry says what was
decided, why, what else was considered, and what would make us revisit it.
Add an entry when a PR makes a choice that someone would otherwise have to
ask about later. Don't rewrite old entries. If a decision changes, add a new
entry that replaces the old one.

---

## D1. Plan on a 3 m grid anchored at the dock

**Status:** accepted (M1)

**Decision.** Users and the agent select survey areas as 3 m cells in a
dock-centered local frame. Connected cells form groups, and the planner works
from the cells.

**Why.** Cells are easy to edit (click, add a box, remove a box), easy to
compare and send between backend and frontend, and the same idea works for
a user's hand selection and a selection made from an OSM feature. The dock as
origin makes every index readable as a distance from home.

**Alternatives.** Free-hand polygons, which are more precise but harder to
edit, merge and subtract reliably. Selecting OSM features directly as
polygons, which can't express "this part of the fairway".

**Revisit if** the camera footprint or the precision we need makes 3 m the
wrong size, or if we cover areas so large that the cell count gets slow.
See [coordinate frame and grid](concepts/coordinate-frame-and-grid.md).

---

## D2. Use an OpenStreetMap snapshot for course data

**Status:** accepted (M1)

**Decision.** Course geometry comes from an OSM snapshot stored in the repo,
not from live queries or hand-traced shapes.

**Why.** It is free and already mapped. A snapshot keeps tests and demos
repeatable and works offline.

**Alternatives.** Live Overpass queries (fresher, but a network dependency
and results that can change). Tracing from our own imagery (accurate, but slow
and a separate job).

**Revisit if** the OSM outlines are wrong in ways that affect a survey, or
once we have our own orthomosaics to trace from.
See [course data](concepts/course-data.md).

---

## D3. The LLM calls the planner and never does geometry itself

**Status:** accepted (M1)

**Decision.** The agent works only through tools that run the same code as
the UI. It never produces coordinates, areas or waypoints itself, and it
cannot start a flight.

**Why.** Geometry has to be correct and testable. Model output is neither.
With this split, the agent's mistakes show up on the map and can be undone,
and a human stays the only one who can start a flight.

**Alternatives.** Letting the model write waypoints directly (fragile and
can't be checked). No agent at all (works, but loses the natural-language
planning the project is about).

**Revisit if** we add tools that act on the real aircraft. Those need their
own confirmation steps.
See [agent](concepts/agent.md).

---

## D4. Boustrophedon decomposition on scanlines, with a sweep angle search

**Status:** accepted (M1, PR #2)

**Decision.** Each group is split into pieces with a scanline version of
boustrophedon cell decomposition, at 36 sweep angles plus the long axis. The best few
angles are planned fully, and pieces are ordered with nearest neighbor plus
2-opt.

**Why.** One set of passes along the long axis flew over the rough on curved
and dogleg fairways (55% of hole 6's path). The scanline version uses the
same split and merge rule as exact BCD, in about 30 lines, on the rows we fly
anyway.

**Alternatives.** Exact BCD on polygon vertices (fewer pieces, much more
code). Grid-based coverage that visits each cell (lots of turns, a poor fit
for a camera that covers about 25 m). Splitting each fairway by hand (doesn't
scale, and the agent couldn't do it).

**Revisit if** piece counts or turn counts become the main cost (for example
once there are battery limits per sortie), or if coverage at the edges turns
out to be too thin.
See [coverage path planning](concepts/coverage-planning.md).

---

## D5. Keep docs in `/docs`, not the GitHub Wiki

**Status:** accepted (2026-11-10)

**Decision.** The concept docs live in `/docs` in this repo and are reviewed
in PRs, with a docs checkbox in the PR template.

**Why.** The GitHub Wiki is a separate git repo. Its edits don't go through
review or CI, and a page can't be tied to the version of the code it
describes. While the planner is changing quickly (M2 and M3), docs that live
next to the code are the only ones that will realistically stay accurate.

**Alternatives.** The GitHub Wiki (easier to edit in the browser).
Publishing `/docs` to the Wiki with an Action (more CI work for little gain
right now).

**Revisit if** the project needs a public-facing site, at which point
`/docs` can be published with GitHub Pages without moving anything.

---

## D6. Run SITL headless in Docker and show it on the planner's map

**Status:** accepted (2026-11-16)

**Decision.** ArduPilot SITL (generic ArduCopter quad) runs headless in a
Docker container on every platform, with MAVLink on TCP ports 5760 (the
planner) and 5762 (an optional ground control station). The planner stays a
native app. The visual demo is the planner's own map plus, optionally,
QGroundControl or Mission Planner on Windows. No 3D simulator.

**Why.** On Windows, Docker Desktop hides WSL2 behind one installer and one
command, and the container only needs TCP ports, which Docker Desktop
forwards to `localhost` reliably. Nothing in the demo needs a Linux GUI, so
WSLg, GPU drivers and X servers are out of the picture. The same image is
used for the headless cloud test, so what is tested is what is demoed. The
planner's map already shows what matters for a survey: the planned passes
and the autopilot's real track.

**Alternatives.** SITL directly in a WSL2 distro (works, but means managing
a distro, its networking and an ArduPilot build by hand). Gazebo with WSLg
(a 3D view of a generic quad, at the cost of a GUI stack, GPU setup and a much
larger install; worth it once there is a real airframe model and a camera to
simulate). Mission Planner's built-in simulator (easy, but downloads its own
SITL build and home location, so it isn't the same setup as the test).

**Revisit if** we need a camera or sensor simulation (then Gazebo), or once
the real airframe has its own SITL model.
See [ArduPilot SITL and MAVLink missions](concepts/sitl.md).

**Update (2026-11-17).** The 3D view came from the planner's map instead of
a simulator: see [D7](#d7-show-flights-in-3d-on-the-planners-map-not-in-gazebo).

## D7. Show flights in 3D on the planner's map, not in Gazebo

**Status:** accepted (2026-11-17)

**Decision.** The 3D view of a flight is drawn in the browser, inside the
planner's existing MapLibre map, with three.js: a quad model at its real
altitude and attitude, the planned passes at survey altitude, the flown
track in 3D, and the dock pad. It is driven only by the telemetry WebSocket,
so it works the same for the built-in sim and for ArduPilot SITL. A 2D / 3D /
Chase toggle switches views. SITL stays headless.

**Why.** It needs nothing new on Windows: no X server, WSLg, GPU passthrough
or second container, just the browser the demo already uses. It sits on the
satellite imagery of the actual course, next to the selection and the plan,
which is what a viewer wants to see. Gazebo would fly the same generic quad
over an empty world unless we built a course model, and its physics wouldn't
be used (SITL flies its own). The model is only a picture: everything it shows
comes from the autopilot's telemetry (position, altitude, heading, roll and
pitch from `ATTITUDE`).

**Alternatives.** Gazebo Harmonic with `ardupilot_gazebo` in Docker, shown
through noVNC or Gazebo's web viewer (software rendering in a container is
slow, the image is several GB, and the plugin build is one more thing to keep
working; the world would still need the course). QGroundControl's or
Mission Planner's map (2D, fine as a second screen). Cesium with 3D terrain
(heavier, and terrain under a flat golf course adds little).

**Limits.** Ground is flat at the dock's height (no terrain), so altitudes
are relative to the dock, as the mission's are. The drone and dock are drawn
enlarged when zoomed out (the legend says how much). Telemetry is 5 Hz, so the
model is smoothed between updates.

**Revisit if** we add camera simulation or obstacle avoidance (then Gazebo
earns its setup), or the site has real elevation changes (then add a terrain
source).
