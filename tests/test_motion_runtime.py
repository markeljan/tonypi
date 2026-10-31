"""Everyday execution stays inside the daemon; all hardware here is fake."""

import queue
import threading
import time
from types import SimpleNamespace

import pytest

from tonypi.motion.library import Frame, Motion
from tonypi.motion import catalog
from tonypi.tonyd.body import Body
from tonypi.tonyd.server import Tonyd
from tonypi.tonyd import server


@pytest.fixture
def everyday(monkeypatch, tmp_path):
    robot = Body.__new__(Body)
    robot.cancelled = threading.Event()
    robot.player = SimpleNamespace(stop=lambda: None)
    robot.board = SimpleNamespace(battery_mv=11500, imu=SimpleNamespace(t=time.monotonic(), accel=(-1, 0, 0)))
    robot.studio = SimpleNamespace(calibration=lambda: {"trim": {}, "limits": {}}, user_dir=tmp_path)
    robot.asleep, robot._abort_reason = False, None
    robot._verify_everyday_calibration = lambda: None
    robot.tilt_deg = lambda: 0.0
    robot.lying = lambda: robot.tilt_deg() > 60
    robot.temps = lambda *args: {i: 40 for i in range(1, 17)}
    calls = []

    def resolve(name, *args, **kwargs):
        names = ["start", "step", "end"] if name == "walk_forward" else [name]
        return [Motion(n, [Frame(20, {1: 500})]) for n in names]

    def motions(moves, say, guard_tilt=None):
        calls.append(([m.name for m in moves], guard_tilt))
        robot._abort_reason = None
        return True

    monkeypatch.setattr(catalog, "resolve_skill", resolve)
    robot._motions = motions
    return robot, calls


@pytest.mark.parametrize("reason", ["cancelled", "stale_imu", "missing_imu", "low_battery", "hot", "missing_temp"])
def test_everyday_refuses_unsafe_start_without_playing(everyday, reason):
    robot, calls = everyday
    if reason == "cancelled":
        robot.cancelled.set()
    elif reason == "stale_imu":
        robot.board.imu.t = time.monotonic() - 2
    elif reason == "missing_imu":
        robot.board.imu = None
    elif reason == "low_battery":
        robot.board.battery_mv = 10899
    elif reason == "hot":
        robot.temps = lambda *args: {1: 70}
    elif reason == "missing_temp":
        robot.temps = lambda *args: {i: None if i == 16 else 40 for i in range(1, 17)}
    with pytest.raises(ValueError):
        robot.run_everyday("walk_forward", 1, lambda _: None)
    assert calls == []


def test_everyday_runs_resolved_segments_with_tilt_guard(everyday):
    robot, calls = everyday
    assert robot.run_everyday("walk_forward", 1, lambda _: None)
    assert calls == [(["start"], 40), (["step"], 40), (["end"], 40)]


def test_cancel_during_segment_never_recovers(everyday):
    robot, calls = everyday

    def cancelled(moves, say, guard_tilt=None):
        calls.append(([m.name for m in moves], guard_tilt))
        robot.cancelled.set()
        robot._abort_reason = "tilt"
        robot.tilt_deg = lambda: 90
        return False

    robot._motions = cancelled
    assert not robot.run_everyday("walk_forward", 1, lambda _: None)
    assert len(calls) == 1


@pytest.mark.parametrize("reason", ["hot", "low_battery", None])
def test_non_tilt_failure_never_recovers(everyday, reason):
    robot, calls = everyday

    def failed(moves, say, guard_tilt=None):
        calls.append(([m.name for m in moves], guard_tilt))
        robot._abort_reason = reason
        return False

    robot._motions = failed
    assert not robot.run_everyday("walk_forward", 1, lambda _: None)
    assert len(calls) == 1


def test_tilt_recovers_once_then_retries_only_interrupted_segment(everyday):
    robot, calls = everyday
    recovered = []
    failed = False

    def motions(moves, say, guard_tilt=None):
        nonlocal failed
        name = moves[0].name
        calls.append(([name], guard_tilt))
        if name == "step" and not failed:
            failed = True
            robot._abort_reason = "tilt"
            return False
        robot._abort_reason = None
        return True

    robot._motions = motions
    robot._recover_everyday = lambda say: recovered.append(True) or True
    assert robot.run_everyday("walk_forward", 1, lambda _: None)
    assert recovered == [True]
    assert [names for names, _ in calls] == [["start"], ["step"], ["step"], ["end"]]


def test_repeated_tilt_exhausts_single_recovery_budget(everyday):
    robot, calls = everyday
    recovered = []

    def motions(moves, say, guard_tilt=None):
        calls.append(([m.name for m in moves], guard_tilt))
        robot._abort_reason = "tilt"
        return False

    robot._motions = motions
    robot._recover_everyday = lambda say: recovered.append(True) or True
    assert not robot.run_everyday("walk_forward", 1, lambda _: None)
    assert recovered == [True]
    assert len(calls) == 2


@pytest.mark.parametrize("z,name", [(0.97, "get_up_front"), (-0.97, "get_up_back")])
def test_get_up_uses_measured_orientation_then_checks_upright(everyday, z, name):
    robot, calls = everyday
    robot.board.imu.accel = (0, 0, z)
    robot.tilt_deg = lambda: 90

    def motions(moves, say, guard_tilt=None):
        calls.append(([m.name for m in moves], guard_tilt))
        robot.tilt_deg = lambda: 0
        return True

    robot._motions = motions
    robot._sleep = lambda _: True
    assert robot.run_everyday("get_up", 1, lambda _: None)
    assert calls == [([name], None)]


def test_get_up_failure_to_become_upright_is_not_success(everyday):
    robot, calls = everyday
    robot.board.imu.accel = (0, 0, 0.97)
    robot.tilt_deg = lambda: 90
    robot._sleep = lambda _: True
    assert not robot.run_everyday("get_up", 1, lambda _: None)
    assert calls == [(["get_up_front"], None)]


@pytest.mark.parametrize("tilt,z", [(90, 0.1), (35, 0.8)])
def test_get_up_refuses_ambiguous_orientation(everyday, tilt, z):
    robot, calls = everyday
    robot.board.imu.accel = (0, 0, z)
    robot.tilt_deg = lambda: tilt
    with pytest.raises(ValueError, match="ambiguous"):
        robot.run_everyday("get_up", 1, lambda _: None)
    assert calls == []


def test_everyday_guard_scope_clears_on_preflight_exception(everyday):
    robot, _ = everyday
    robot.board.battery_mv = 10800
    with pytest.raises(ValueError, match="battery"):
        robot.run_everyday("walk_forward", 1, lambda _: None)
    assert not robot._everyday_active


def test_completed_falling_segment_is_not_replayed(everyday):
    robot, calls = everyday
    recovered = []

    def motions(moves, say, guard_tilt=None):
        calls.append(([m.name for m in moves], guard_tilt))
        if moves[0].name == "step":
            robot.tilt_deg = lambda: 90
        return True

    def recover(say):
        recovered.append(True)
        robot.tilt_deg = lambda: 0
        return True

    robot._motions, robot._recover_everyday = motions, recover
    assert robot.run_everyday("walk_forward", 1, lambda _: None)
    assert recovered == [True]
    assert [names for names, _ in calls] == [["start"], ["step"], ["end"]]


def test_failed_recovery_never_resumes_original_motion(everyday):
    robot, calls = everyday

    def failed(moves, say, guard_tilt=None):
        calls.append(([m.name for m in moves], guard_tilt))
        robot._abort_reason = "tilt"
        return False

    robot._motions = failed
    robot._recover_everyday = lambda say: False
    assert not robot.run_everyday("walk_forward", 1, lambda _: None)
    assert len(calls) == 1


def test_cancellation_during_get_up_prevents_success(everyday):
    robot, calls = everyday
    robot.board.imu.accel = (0, 0, 0.97)
    robot.tilt_deg = lambda: 90

    def cancelled(moves, say, guard_tilt=None):
        calls.append(([m.name for m in moves], guard_tilt))
        robot.cancelled.set()
        robot.tilt_deg = lambda: 0
        return True

    robot._motions = cancelled
    assert not robot.run_everyday("get_up", 1, lambda _: None)
    assert robot.cancelled.is_set()
    assert len(calls) == 1


def test_temperature_rise_at_fall_prevents_get_up(everyday):
    robot, calls = everyday

    def fallen(moves, say, guard_tilt=None):
        calls.append(([m.name for m in moves], guard_tilt))
        robot._abort_reason = "tilt"
        robot.temps = lambda *args: {i: 70 for i in range(1, 17)}
        return False

    robot._motions = fallen
    with pytest.raises(ValueError, match="temperature"):
        robot.run_everyday("walk_forward", 1, lambda _: None)
    assert len(calls) == 1


@pytest.mark.parametrize("count", [True, False, 0, 6, -1, 1.5, "2", None])
def test_everyday_submit_rejects_invalid_count_before_touching_body(count):
    daemon = Tonyd.__new__(Tonyd)
    with pytest.raises(ValueError, match="count"):
        daemon.submit("walk_forward", count, everyday=True)


def test_everyday_submit_rejects_custom_frames_before_touching_body():
    daemon = Tonyd.__new__(Tonyd)
    with pytest.raises(ValueError, match="learning"):
        daemon.submit("wave", frames=[{"ms": 20, "pos": {1: 500}}], everyday=True)


def test_everyday_submit_honors_stop_latch_before_queueing(everyday):
    robot, _ = everyday
    daemon = Tonyd.__new__(Tonyd)
    daemon.body, daemon.stop_latched = robot, True
    with pytest.raises(ValueError, match="stop latch"):
        daemon.submit("walk_forward", everyday=True)
    assert not robot.cancelled.is_set()


def test_everyday_submit_honors_tether_before_queueing(everyday):
    robot, _ = everyday
    daemon = Tonyd.__new__(Tonyd)
    daemon.body, daemon.stop_latched, daemon.walked = robot, False, 10
    daemon.tether = lambda: {"on": True, "max_steps": 10}
    with pytest.raises(ValueError, match="tethered"):
        daemon.submit("walk_forward", everyday=True)
    assert daemon.walked == 10
    assert not robot.cancelled.is_set()


@pytest.mark.parametrize("latch", [False, True])
def test_stop_after_dequeue_before_worker_clear_does_not_restart_motion(everyday, monkeypatch, tmp_path, latch):
    robot, calls = everyday
    monkeypatch.setattr(server, "JOBS_LOG", tmp_path / "jobs.jsonl")
    daemon = Tonyd(robot)
    dequeued = threading.Event()

    class ObservedQueue(queue.Queue):
        def get(self, *args, **kwargs):
            job = super().get(*args, **kwargs)
            dequeued.set()
            return job

    daemon.queue = ObservedQueue(maxsize=1)
    daemon.lock.acquire()
    job = daemon.submit("get_up", everyday=True)
    threading.Thread(target=daemon._worker, daemon=True).start()
    assert dequeued.wait(1)
    daemon.stop(latch=latch)
    daemon.lock.release()
    assert job.finished.wait(1)
    assert job.state == "cancelled"
    assert robot.cancelled.is_set()
    assert daemon.current is None
    assert calls == []


@pytest.mark.parametrize("battery", [10899, None])
def test_watch_low_battery_midmotion_stops_unloads_and_never_recovers(everyday, battery):
    robot, calls = everyday
    stopped, relaxed = [], []
    robot._everyday_active = True
    robot.board.battery_mv = battery
    robot.player = SimpleNamespace(stop=lambda: stopped.append(True))
    robot.relax = lambda: relaxed.append(True)
    robot._watch([1], 40, threading.Event())
    assert robot._abort_reason == "low_battery"
    assert stopped == relaxed == [True]
    assert calls == []


def test_watch_cancellation_takes_precedence_over_low_battery(everyday):
    robot, calls = everyday
    stopped, relaxed = [], []
    robot._everyday_active = True
    robot.board.battery_mv = 10800
    robot.cancelled.set()
    robot.player = SimpleNamespace(stop=lambda: stopped.append(True))
    robot.relax = lambda: relaxed.append(True)
    robot._watch([1], 40, threading.Event())
    assert robot._abort_reason is None
    assert stopped == [True] and relaxed == []
    assert calls == []


def test_everyday_preserves_peak_tilt_over_all_segments(everyday):
    robot, _ = everyday
    readings = iter([(19, 23), (8, 9), (3, 4)])
    def motions(*args):
        robot.max_tilt, robot.max_tilt_raw = next(readings)
        return True
    robot._motions = motions
    assert robot.run_everyday("walk_forward", 1, lambda _: None)
    assert (robot.max_tilt, robot.max_tilt_raw) == (19, 23)


def test_servo_calibration_drift_refuses_before_motion(everyday, monkeypatch):
    from tonypi.motion import catalog
    robot, calls = everyday
    robot._verify_everyday_calibration = Body._verify_everyday_calibration.__get__(robot, Body)
    monkeypatch.setattr(catalog, "manifest", lambda: {"hardware_calibration_snapshot": {"1": {"offset": 0, "limits": [0, 1000]}}})
    robot.servo_calibration = lambda: {1: {"offset": 1, "limits": [0, 1000]}}
    with pytest.raises(ValueError, match="EEPROM calibration"):
        robot.run_everyday("wave", 1, lambda _: None)
    assert calls == []


def test_standing_precondition_refuses_tilted_pose(everyday):
    robot, calls = everyday
    robot.tilt_deg = lambda: 35
    with pytest.raises(ValueError, match="standing start"):
        robot.run_everyday("wave", 1, lambda _: None)
    assert calls == []
