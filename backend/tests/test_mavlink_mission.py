import math

import pytest
from pymavlink import mavutil

from app import planner
from app.course import Course
from app.mavlink_mission import build_mission, nav_seqs
from app.state import MissionState

mav = mavutil.mavlink


@pytest.fixture(scope="module")
def course():
    return Course()


def test_mission_shape(course):
    route = [(0.0, 0.0), (10.0, 0.0), (10.0, 0.2), (10.0, 20.0), (0.0, 0.0)]
    items = build_mission(route, course.frame)
    cmds = [it.command for it in items]
    assert cmds == [
        mav.MAV_CMD_NAV_WAYPOINT,  # home
        mav.MAV_CMD_NAV_TAKEOFF,
        mav.MAV_CMD_DO_CHANGE_SPEED,
        mav.MAV_CMD_NAV_WAYPOINT,
        mav.MAV_CMD_NAV_WAYPOINT,  # (10, 0.2) dropped: under MIN_LEG_M from (10, 0)
        mav.MAV_CMD_NAV_LAND,
    ]
    assert [it.seq for it in items] == list(range(len(items)))
    assert items[1].alt == planner.ALTITUDE_M
    assert items[2].params[1] == planner.CRUISE_SPEED_MS
    assert all(it.alt == planner.ALTITUDE_M for it in items[3:-1])
    # Waypoints land back on the planner's points, and the landing is on the dock.
    x, y = course.frame.to_xy(items[4].lon, items[4].lat)
    assert math.dist((x, y), (10.0, 20.0)) < 0.05
    assert (items[-1].lat, items[-1].lon) == (course.frame.lat0, course.frame.lon0)
    assert nav_seqs(items) == [3, 4]


def test_message_encoding_keeps_1e7_precision(course):
    items = build_mission([(0.0, 0.0), (123.4, -56.7), (0.0, 0.0)], course.frame)
    msg = items[3].to_msg(1, 1)
    assert msg.x == round(items[3].lat * 1e7) and msg.y == round(items[3].lon * 1e7)
    assert msg.frame == mav.MAV_FRAME_GLOBAL_RELATIVE_ALT and msg.autocontinue == 1
    msg.pack(mav.MAVLink(None))  # encodes without a range error


def test_real_route_fits_in_a_mission(course):
    state = MissionState(course)
    state.edit("add", course.feature_cells(6, "fairway", 30.0))
    state.preview()
    route = state.route()
    items = build_mission(route["waypoints"], course.frame)
    # Copter SITL stores ~700 items; a whole hole is far below that.
    assert 10 < len(items) < 200
    assert len(nav_seqs(items)) == len(items) - 4
