import math

import pytest

from app.course import DOCK_LAT, DOCK_LON, Course
from app.geo import LocalFrame, cell_of
from app.planner import components, preview_path, regroup
from app.state import MissionState


@pytest.fixture(scope="module")
def course():
    return Course()


def test_frame_roundtrip():
    f = LocalFrame(DOCK_LAT, DOCK_LON)
    lon, lat = f.to_lonlat(300.0, -450.0)
    x, y = f.to_xy(lon, lat)
    assert math.isclose(x, 300.0, abs_tol=1e-6) and math.isclose(y, -450.0, abs_tol=1e-6)
    assert cell_of(0.1, 0.1) == (0, 0)
    assert cell_of(-0.1, 2.9) == (-1, 0)


def test_front_nine_loaded(course):
    assert sorted(course.holes) == list(range(1, 10))
    for h in course.holes.values():
        assert h.fairway is not None and h.green is not None
    # Hole 1's line starts at the dock tee.
    x, y = course.holes[1].line.coords[0]
    assert math.hypot(x, y) < 2
    # Areas roughly match the plan's OSM table (fairways minus greens).
    assert 4500 < course.holes[4].fairway.area < 6500


def test_four_connectivity():
    cells = {(0, 0), (1, 0), (2, 2), (3, 3)}  # diagonal touch stays separate
    sizes = sorted(len(c) for c in components(cells))
    assert sizes == [1, 1, 2]


def test_group_ids_stable():
    a = {(i, 0) for i in range(5)}
    b = {(i, 10) for i in range(5)}
    g1 = regroup(a | b, {})
    gid_b = next(k for k, v in g1.items() if (0, 10) in v)
    # Editing group A (shrinking it) must not renumber group B.
    g2 = regroup((a - {(4, 0)}) | b, g1)
    assert next(k for k, v in g2.items() if (0, 10) in v) == gid_b
    # A new disconnected piece gets the next free number.
    g3 = regroup(a | b | {(50, 50)}, g2)
    assert set(g3) == {1, 2, 3}


def test_hole4_fairway_selection_and_preview(course):
    s = MissionState(course)
    cells = course.feature_cells(4, "fairway")
    s.edit("add", cells)
    assert len(s.groups) == 2  # main fairway + approach, slivers dropped
    s.preview()
    snap = s.snapshot()
    assert snap["route"]["total_m"] > snap["route"]["survey_m"] > 0
    for g in snap["groups"]:
        assert g["preview"]["passes"] >= 2
        assert g["holes"] == [4]
    # Editing a group drops its stale preview.
    any_cell = next(iter(s.groups[1]))
    s.edit("remove", {any_cell})
    assert 1 not in s.previews or s.previews[1]._cells == s.groups[1]


def test_preview_covers_rectangle():
    cells = {(i, j) for i in range(30) for j in range(10)}  # 90 m x 30 m
    p = preview_path(cells, spacing_m=7.5)
    assert p.passes == 4  # 30 m / 7.5 m
    assert p.heading_deg in (90, 270) or round(p.heading_deg) == 90
    assert 300 < p.length_m < 400
