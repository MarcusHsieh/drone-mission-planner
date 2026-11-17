"""OSM buildings around the course, as GeoJSON with a height for each.

The 3D view extrudes these into boxes: the free fallback and the placeholder
while Google's photorealistic tiles load. Data comes from, in order:

1. app/data/osm_buildings.json, a snapshot committed to the repo
   (`uv run python -m tools.fetch_buildings` writes it),
2. var/osm_buildings.json, a cache written on the first request,
3. the Overpass API, once, to fill that cache.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from pathlib import Path
from typing import Any

import httpx

from . import config
from .course import Course
from .geo import CELL_M

log = logging.getLogger(__name__)

SNAPSHOT = Path(__file__).resolve().parent / "data" / "osm_buildings.json"
OVERPASS_URL = "https://overpass-api.de/api/interpreter"
MARGIN_M = 300.0  # around the course grid
LEVEL_M = 3.0
# Without height or building:levels, guess from the building type.
DEFAULT_HEIGHT_M = {"house": 6.0, "detached": 6.0, "residential": 7.0, "garage": 3.0, "garages": 3.0, "shed": 2.5, "roof": 4.0, "carport": 3.0}
FALLBACK_HEIGHT_M = 6.0


def bbox(course: Course) -> tuple[float, float, float, float]:
    """(south, west, north, east) of the course grid plus a margin."""
    i0, j0, i1, j1 = course.cell_bounds
    c = course.frame
    w, s = c.to_lonlat(i0 * CELL_M - MARGIN_M, j0 * CELL_M - MARGIN_M)
    e, n = c.to_lonlat(i1 * CELL_M + MARGIN_M, j1 * CELL_M + MARGIN_M)
    return s, w, n, e


def _meters(v: str | None) -> float | None:
    if not v:
        return None
    m = re.match(r"\s*([0-9]+(?:\.[0-9]+)?)", v)
    if not m:
        return None
    x = float(m.group(1))
    return x * 0.3048 if ("ft" in v or "'" in v) else x


def height_of(tags: dict[str, str]) -> tuple[float, float]:
    """(height, min_height) in meters."""
    h = _meters(tags.get("height"))
    if h is None and (lv := _meters(tags.get("building:levels"))) is not None:
        h = lv * LEVEL_M + 1.0
    if h is None:
        h = DEFAULT_HEIGHT_M.get(tags.get("building", ""), FALLBACK_HEIGHT_M)
    mh = _meters(tags.get("min_height")) or 0.0
    return max(h, mh + 1.0), mh


def to_geojson(overpass: dict[str, Any]) -> dict[str, Any]:
    features = []
    for el in overpass.get("elements", []):
        if el.get("type") != "way" or "geometry" not in el:
            continue
        ring = [[p["lon"], p["lat"]] for p in el["geometry"]]
        if len(ring) < 4 or ring[0] != ring[-1]:
            continue
        tags = el.get("tags", {})
        h, mh = height_of(tags)
        features.append({
            "type": "Feature",
            "properties": {"osm_way": el["id"], "height": round(h, 1), "min_height": round(mh, 1), "building": tags.get("building", "yes")},
            "geometry": {"type": "Polygon", "coordinates": [ring]},
        })  # fmt: skip
    return {"type": "FeatureCollection", "features": features}


def fetch(course: Course, timeout: float = 60.0) -> dict[str, Any]:
    s, w, n, e = bbox(course)
    query = f"[out:json][timeout:50];way[building]({s:.6f},{w:.6f},{n:.6f},{e:.6f});out tags geom;"
    r = httpx.post(OVERPASS_URL, data={"data": query}, timeout=timeout, headers={"User-Agent": "drone-mission-planner"})
    r.raise_for_status()
    return to_geojson(r.json())


class Buildings:
    def __init__(self, course: Course) -> None:
        self.course = course
        self._lock = threading.Lock()
        self._data: dict[str, Any] | None = None
        self.source = ""
        self.error: str | None = None

    def get(self) -> dict[str, Any]:
        with self._lock:
            if self._data is None:
                data = self._load()
                if data.pop("retry", False):
                    return {"source": "", "error": self.error, **data}
                self.error = None
                self._data = data
            return {"source": self.source, "error": None, **self._data}

    def _load(self) -> dict[str, Any]:
        cache = config.DATA_DIR / "osm_buildings.json"
        for path, source in ((SNAPSHOT, "snapshot"), (cache, "cache")):
            if path.exists():
                self.source = source
                return json.loads(path.read_text())
        try:
            data = fetch(self.course)
        except (httpx.HTTPError, ValueError) as e:
            # Try again on the next page load rather than caching the failure.
            self.error = f"Could not fetch OSM buildings from Overpass: {e}"
            log.warning(self.error)
            return {"type": "FeatureCollection", "features": [], "retry": True}
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(data))
        self.source = "overpass"
        log.info("Fetched %d OSM buildings into %s", len(data["features"]), cache)
        return data
