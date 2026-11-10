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
