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
| **Sortie** | One flight on one battery charge. M1 has no sorties yet: a route is assumed to fit on a single battery. Splitting into sorties with charge stops is planned for M2 and M3. |

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
