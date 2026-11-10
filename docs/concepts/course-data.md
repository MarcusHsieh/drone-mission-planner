# Course data

## In this project

The map of the course (holes, fairways, greens, tees, bunkers) comes from a
snapshot of OpenStreetMap that is stored in the repo. When the backend starts,
it loads the snapshot, converts it into the
[local frame](coordinate-frame-and-grid.md), and matches each fairway, green
and tee to a hole. "Select the fairway on hole 4" works because of this
matching.

| What | Where |
| --- | --- |
| Snapshot | [`backend/app/data/osm_front9.json`](../../backend/app/data/osm_front9.json) |
| Loading, decoding, matching features to holes | [`backend/app/course.py`](../../backend/app/course.py) (`Course._load`, `Course._assign`) |
| Feature → cells, sliver removal | `Course.cells_in`, `Course.feature_cells` |
| Dock location | `DOCK_LAT`, `DOCK_LON` in `course.py` |

The course is the front nine of San Ramon Golf Club, OSM relation
[3566976](https://www.openstreetmap.org/relation/3566976). The snapshot was
read on 2026-11-08.

## Background

### Golf in OpenStreetMap

OSM maps golf courses with `golf=*` tags:

- `golf=hole`: a **line** from tee to green, with `ref` (the hole number) and
  `par`. It is the only feature that carries the hole number.
- `golf=fairway`, `golf=green`, `golf=tee`, `golf=bunker`, and so on:
  **areas** (closed ways or multipolygons). They usually carry no hole number.

So "the fairway of hole 4" is not stored anywhere in OSM. It has to be worked
out from the geometry.

### Snapshot vs live queries

Reading OSM live (through the Overpass API) gets the latest edits, but it adds
a network dependency, rate limits, and results that can change between runs.
A snapshot that is checked into the repo keeps tests and demos repeatable.
The cost is that it has to be refreshed by hand.

## Our choices and limits

- **The snapshot format is compact on purpose.** Each entry is
  `[kind, ref, par, osm_id, rings]`. Each ring is `role|x0,y0;dx,dy;...` in
  micro-degrees, with each point stored as the difference from the previous
  one, which keeps the file small. The format is documented in the
  `course.py` docstring. Read that if you need to regenerate the file.
- **Matching rules** (`Course._assign`):
  - A **fairway** goes to the hole whose tee-to-green line runs through it the
    furthest (with the fairway grown by 5 m). It needs at least 15 m of
    overlap, otherwise it stays unmatched.
  - A **green** goes to the hole whose line *ends* nearest to it, within
    25 m. Each hole gets one green.
  - A **tee** goes to the hole whose line *starts* nearest to it, within 60 m.
    A hole can have several tees.
  These rules work for the front nine as it is mapped now. A hole line that is
  drawn badly in OSM (for example ending short of the green) will quietly give
  the wrong match, so check the map after refreshing the snapshot.
- **Fairways are often split in two.** OSM often maps the main fairway and the
  approach to the green as separate shapes with a gap between them. When
  that happens, one hole's fairway becomes two groups (hole 4 in the demo
  script). The agent's instructions tell it to mention this.
- **Slivers are dropped** (`feature_cells`, `min_piece_cells=10`). Fairways
  cut around greens leave thin edges that turn into 1 to 4 cell islands. When
  a fairway or corridor is converted to cells, any piece under 10 cells
  (90 m²) is dropped. Greens and tees keep every cell, because they are small
  to begin with.
- **Corridors for unmapped holes.** If a hole has no fairway mapped, or the
  user asks for a strip, `feature="corridor"` uses a 40 m wide band along the
  hole line (`DEFAULT_CORRIDOR_M`).
- **The outlines may be out of date.** The OSM outlines were traced from old
  Yahoo imagery. Check them against the satellite layer. If a hole is off, fix
  it in OSM and refresh the snapshot. Don't patch the JSON by hand.
- **Front nine only.** Holes 10 to 18 are dropped while loading
  (`int(ref) > 9`). The grid covers everything within 60 m of a front-nine
  feature.
- **The dock location is hard-coded.** It is the center of the hole 1 back tee
  (OSM way 934037995). Moving the dock moves the whole frame (see
  [coordinate frame](coordinate-frame-and-grid.md)).
