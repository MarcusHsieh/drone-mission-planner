from app import vitals
from app.vitals import mavlink


def test_message_log_ids_and_tail():
    log = vitals.MessageLog(maxlen=3)
    for k in range(5):
        log.add(f"m{k}", vitals.WARNING)
    log.add("   ")  # ignored
    assert [m["text"] for m in log.all()] == ["m2", "m3", "m4"]
    assert [m["id"] for m in log.tail(2)] == [4, 5] and log.last_id == 5
    assert log.tail(1)[0]["level"] == "WARNING"


def test_sensor_health_only_lists_present_sensors():
    gyro, mag = mavlink.MAV_SYS_STATUS_SENSOR_3D_GYRO, mavlink.MAV_SYS_STATUS_SENSOR_3D_MAG
    out = vitals.sensor_health(gyro | mag, gyro | mag, gyro)
    assert out == [{"name": "Gyro", "enabled": True, "healthy": True}, {"name": "Compass", "enabled": True, "healthy": False}]


def test_prearm_bit():
    b = vitals.PREARM_BIT
    assert vitals.prearm_status(0, 0, 0, [])["ok"] is None
    assert vitals.prearm_status(b, b, 0, ["PreArm: x"]) == {"ok": False, "reasons": ["PreArm: x"]}
    assert vitals.prearm_status(b, b, b, [])["ok"] is True


def test_ekf_levels():
    good = sum(vitals.EKF_REQUIRED.values())
    assert vitals.ekf_status(good, {"compass": 0.1})["level"] == "ok"
    assert vitals.ekf_status(good, {"compass": 0.6})["level"] == "warn"
    assert vitals.ekf_status(good, {"compass": 0.9})["level"] == "bad"
    r = vitals.ekf_status(good & ~mavlink.EKF_POS_HORIZ_ABS, {})
    assert r["level"] == "bad" and r["missing"] == ["pos_horiz_abs"]


def test_vibration_levels():
    assert vitals.vibration_level(10, 12, 20) == "ok"
    assert vitals.vibration_level(10, 45, 20) == "warn"
    assert vitals.vibration_level(10, 12, 70) == "bad"
