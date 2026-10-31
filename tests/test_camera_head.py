"""Camera/head concurrency uses fake hardware and synchronization, never the robot."""
import threading
import time

import cv2
import numpy as np
import pytest

from tonypi.tonyd.camera import Camera
from tonypi.tonyd.client import TonydError
from tonypi.tonyd.server import Tonyd
from test_tonyd import _Body, robot, _private_job_log


@pytest.mark.parametrize('operation', ['burst', 'scan', 'snapshot'])
@pytest.mark.parametrize('command', ['head', 'stop'])
def test_camera_request_does_not_queue_head_or_stop_on_same_socket(robot, command, operation):
    d, c = robot
    entered, release = threading.Event(), threading.Event()
    def burst(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return {'frames': []}
    setattr(d, operation, burst)
    result = []
    thread = threading.Thread(target=lambda: result.append(c.call(operation)))
    thread.start()
    try:
        assert entered.wait(1)
        started = time.monotonic()
        if command == 'head':
            assert c.call('head', timeout=0.5, pan_deg=5)['head_deg']['pan'] == 5
        else:
            assert c.call('stop', timeout=0.5, latch=True)['latched']
        assert time.monotonic() - started < 0.5
        assert not d.state()['busy'] and not d.recent
    finally:
        release.set()
        thread.join(2)
    assert result == [{'frames': []}]


def test_freshness_filter_never_waits_or_returns_old_frame():
    cam = Camera()
    cam._frame = np.zeros((10, 10, 3), np.uint8)
    cam._t = time.monotonic()
    started = time.monotonic()
    assert cam.jpeg(newer_than=started + 10) is None
    assert time.monotonic() - started < 0.1


def test_head_obeys_stop_latch(robot):
    d, c = robot
    c.stop(latch=True)
    with pytest.raises(TonydError, match='stop latch'):
        c.head(5, 0)
    assert not d.body.board.heads


def test_exposure_never_blocks_head_and_snapshot_keeps_capture_pose(monkeypatch, tmp_path):
    from tonypi.tonyd import server
    entered, release = threading.Event(), threading.Event()
    cam = Camera()
    d = Tonyd(_Body(), cam)
    class Capture:
        def read(self):
            entered.set()
            assert release.wait(2)
            cam.stop()
            return True, np.zeros((10, 10, 3), np.uint8)
        def release(self):
            pass
    monkeypatch.setattr(cam, '_open', lambda: Capture())
    monkeypatch.setattr(server, 'SNAP_DIR', tmp_path)
    cam._running = True
    thread = threading.Thread(target=cam._loop)
    thread.start()
    try:
        assert entered.wait(1)
        d.head(5, -2, ms=100)  # succeeds while cap.read is stalled
        assert not d.state()['busy'] and not d.recent
    finally:
        release.set()
        thread.join(2)
    first = d.snapshot()
    assert first['head_moving'] and first['head_deg'] == {'pan': 5, 'tilt': -2}
    assert first['captured_monotonic_s'] >= first['capture_started_monotonic_s']
    assert abs(first['captured_epoch_s'] - time.time()) < 1
    assert first['head_pose_known']
    encode = cv2.imencode
    def encode_during_turn(*args):
        d.head(-5, 2, ms=100)
        return encode(*args)
    monkeypatch.setattr(cv2, 'imencode', encode_during_turn)
    later = d.snapshot()
    assert d.head_deg == {'pan': -5, 'tilt': 2}
    assert later['head_deg'] == first['head_deg']
    assert later['captured_monotonic_s'] == first['captured_monotonic_s']
    assert later['captured_epoch_s'] == first['captured_epoch_s']


def test_camera_capacity_is_bounded_but_head_and_stop_still_work(robot):
    d, c = robot
    entered, release = threading.Semaphore(0), threading.Event()
    def burst(*args, **kwargs):
        entered.release()
        release.wait(3)
        return {'frames': []}
    d.burst = burst
    threads = [threading.Thread(target=c.burst) for _ in range(2)]
    for thread in threads:
        thread.start()
    try:
        assert entered.acquire(timeout=1) and entered.acquire(timeout=1)
        with pytest.raises(TonydError, match='capacity'):
            c.snapshot()
        assert c.head(1, 1)['pan'] == 1
        c.stop(latch=True)
    finally:
        release.set()
        for thread in threads:
            thread.join(2)


@pytest.mark.parametrize('command', ['head', 'stop'])
def test_scan_is_cancelled_without_restoring_over_stop_or_new_head(robot, command):
    d, c = robot
    d.camera = Camera()
    c.head(0, 0)
    entered = threading.Event()
    send = d.body.board.head
    def head(ms, pulses):
        send(ms, pulses)
        entered.set()
    d.body.board.head = head
    errors = []
    def scan():
        try:
            c.scan([10, -10])
        except TonydError as exc:
            errors.append(str(exc))
    thread = threading.Thread(target=scan)
    thread.start()
    try:
        assert entered.wait(1)
        if command == 'head':
            c.head(3, 0)
        else:
            c.stop(latch=True)
        thread.join(1)
        assert not thread.is_alive()
        assert errors and 'cancelled' in errors[0]
        assert len(d.body.board.heads) == (3 if command == 'head' else 2)
    finally:
        thread.join(2)


def test_legacy_head_gestures_use_capture_timeline():
    d = Tonyd(_Body())
    d.body.head_command(100, {2: 1466, 1: 1500})
    state = d.head_state()
    assert state['head_revision'] == 1 and state['head_moving']
    assert state['head_deg'] == {'pan': 0, 'tilt': 0}


@pytest.mark.parametrize('command', ['stop', 'head'])
def test_delayed_scan_cannot_start_after_newer_stop_or_head(robot, command):
    d, c = robot
    d.camera = Camera()
    entered, release = threading.Event(), threading.Event()
    original = d.scan
    def delayed(*args, **kwargs):
        entered.set()
        assert release.wait(2)
        return original(*args, **kwargs)
    d.scan = delayed
    errors = []
    def request():
        try:
            c.scan([5])
        except TonydError as exc:
            errors.append(str(exc))
    thread = threading.Thread(target=request)
    thread.start()
    try:
        assert entered.wait(1)
        if command == 'stop':
            c.stop(latch=False)
        else:
            c.head(2, 1)
        before = len(d.body.board.heads)
    finally:
        release.set()
        thread.join(2)
    assert errors and 'cancelled before start' in errors[0]
    assert len(d.body.board.heads) == before


def test_legacy_one_axis_write_does_not_move_unknown_other_axis():
    d = Tonyd(_Body())
    d.body.head_command(100, {1: 1550})
    assert d.body.board.heads == [{1: 1550}]
    assert not d.head_state()['head_pose_known']
    d.stop(latch=False)
    with pytest.raises(ValueError, match='cancelled'):
        d.body.head_command(100, {1: 1500})
    assert len(d.body.board.heads) == 1


def test_latched_stop_survives_daemon_restart_without_accepting_head():
    first = Tonyd(_Body())
    first.stop(latch=True)
    restarted = Tonyd(_Body())
    assert restarted.stop_latched
    with pytest.raises(ValueError, match='stop latch'):
        restarted.head(5, 0)
    assert not restarted.body.board.heads
    restarted.unlatch()
    assert not Tonyd(_Body()).stop_latched


def test_snapshot_files_stay_bounded_when_encoders_finish_out_of_order(monkeypatch, tmp_path):
    from tonypi.tonyd import server
    entered, release = threading.Event(), threading.Event()
    class Cam:
        width, height = 640, 480
        def snapshot(self, path, **kwargs):
            if path.name == 'snap-000001.jpg':
                entered.set()
                assert release.wait(3)
            path.write_bytes(b'jpeg')
            return {'frame_age_s': 0.01}
    monkeypatch.setattr(server, 'SNAP_DIR', tmp_path)
    d = Tonyd(_Body(), Cam())
    thread = threading.Thread(target=d.snapshot)
    thread.start()
    try:
        assert entered.wait(1)
        for _ in range(50):
            d.snapshot()
    finally:
        release.set()
        thread.join(2)
    assert len(list(tmp_path.glob('snap-*.jpg'))) == server.KEEP_SNAPSHOTS
    assert (tmp_path / 'snap-000001.jpg').exists()
