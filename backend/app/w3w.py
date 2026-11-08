"""what3words lookup for one grid cell, cached in SQLite.

Our cells do not line up with w3w squares, so the address is for the w3w
square under the cell's center. One API call per cell, ever.
"""

from __future__ import annotations

import sqlite3
import threading
import time

import httpx

from . import config
from .course import Course
from .geo import Cell, cell_center

API_URL = "https://api.what3words.com/v3/convert-to-3wa"


class W3WCache:
    def __init__(self, course: Course) -> None:
        self.course = course
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(config.DATA_DIR / "w3w_cache.sqlite3", check_same_thread=False)
        self._db.execute("CREATE TABLE IF NOT EXISTS w3w (i INT, j INT, words TEXT, fetched_at REAL, PRIMARY KEY (i, j))")
        self._lock = threading.Lock()

    def lookup(self, cell: Cell) -> dict:
        lon, lat = self.course.frame.to_lonlat(*cell_center(cell))
        base = {"cell": list(cell), "lat": round(lat, 7), "lon": round(lon, 7)}
        with self._lock:
            row = self._db.execute("SELECT words FROM w3w WHERE i=? AND j=?", cell).fetchone()
        if row:
            return {**base, "words": row[0], "cached": True}
        if not config.W3W_API_KEY:
            return {**base, "words": None, "error": "what3words is not configured (set W3W_API_KEY in backend/.env)"}
        try:
            r = httpx.get(API_URL, params={"coordinates": f"{lat},{lon}", "key": config.W3W_API_KEY}, timeout=8)
            data = r.json()
        except (httpx.HTTPError, ValueError) as e:
            return {**base, "words": None, "error": f"what3words request failed: {e}"}
        if "words" not in data:
            msg = data.get("error", {}).get("message", f"HTTP {r.status_code}")
            return {**base, "words": None, "error": f"what3words: {msg}"}
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO w3w VALUES (?, ?, ?, ?)", (*cell, data["words"], time.time()))
            self._db.commit()
        return {**base, "words": data["words"], "cached": False}
