"""Snapshot the OSM buildings around the course into the repo.

    uv run python -m tools.fetch_buildings

Writes app/data/osm_buildings.json (commit it). The backend uses that file
first, so the 3D view's building boxes then work offline and don't depend on
the Overpass API.
"""

from __future__ import annotations

import json

from app.buildings import SNAPSHOT, bbox, fetch
from app.course import Course


def main() -> None:
    course = Course()
    data = fetch(course)
    SNAPSHOT.write_text(json.dumps(data, separators=(",", ":")))
    print(f"{len(data['features'])} buildings in {bbox(course)} -> {SNAPSHOT}")


if __name__ == "__main__":
    main()
