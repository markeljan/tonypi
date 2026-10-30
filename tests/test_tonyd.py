import tempfile
import threading
import time
from pathlib import Path

import pytest

from tonypi.agent.brain import ACTIONS
from tonypi.tonyd import server
from tonypi.tonyd.client import TonydClient, TonydError
from tonypi.tonyd.server import Tonyd
from tonypi.tonyd.skills import SKILLS


class _Imu:
    def __init__(self):
        self.accel, self.t = (-0.97, 0.0, 0.0), time.monotonic()


class _Board:
    def __init__(self):
        self.imu, self.battery_mv, self.heads = _Imu(), 11500, []

    def head(self, ms, pulses):
        self.heads.append(pulses)


class _Body:
    """Skills take `seconds` each unless cancelled; records what ran."""

    def __init__(self, seconds=0.05):
        self.board, self.cancelled, self.asleep = _Board(), threading.Event(), False
        self.ran, self.relaxed, self.seconds = [], 0, seconds

    def cancel(self):
        self.cancelled.set()

    def run(self, skill, count, say):
        self.ran.append((skill, count))
        if skill == "squat":
            say("My left knee is too hot, at 82 degrees. I need to cool down first.")
            return False
        return not self.cancelled.wait(self.seconds)

    def settle(self):
        self.ran.append(("settle", 1))

    def relax(self):
        self.relaxed += 1

    def tilt_deg(self):
        return 2.0

    def lying(self):
        return False

    def status(self):
        return {"torque_on": 0, "temps": {1: 40}, "hottest": (1, "left ankle", 40), "battery_mv": 11500}


@pytest.fixture
def robot():
    sock = Path(tempfile.mkdtemp()) / "tonyd.sock"
    d = Tonyd(_Body()).start()
    threading.Thread(target=d.serve, args=(str(sock),), daemon=True).start()
    for _ in range(100):
        if sock.exists():
            break
        time.sleep(0.01)
    return d, TonydClient(str(sock))


def test_skills_match_the_llm_actions():
    agent_only = {"describe_view", "report_status", "stop"}
    assert set(ACTIONS) - agent_only == set(SKILLS) - {"settle"}


def test_run_waits_and_reports(robot):
    d, c = robot
    job = c.run("wave", 2)
    assert job["state"] == "done" and job["skill"] == "wave" and job["count"] == 2
    assert d.body.ran == [("wave", 2)]
    job = c.run("squat")
    assert job["state"] == "failed" and "too hot" in job["said"][0]


def test_unknown_skill_and_count_clamp(robot):
    d, c = robot
    with pytest.raises(TonydError, match="unknown skill"):
        c.run("fly")
    assert c.run("walk_forward", 99)["count"] == 5


def test_newest_preempts_and_stop_cancels(robot):
    d, c = robot
    d.body.seconds = 5
    first = c.run("dance", wait=False)
    time.sleep(0.1)
    t = time.monotonic()
    second = c.run("bow", wait=False)
    time.sleep(0.1)
    c.stop()
    for _ in range(50):
        if not c.state()["busy"]:
            break
        time.sleep(0.05)
    assert time.monotonic() - t < 2
    assert [s for s, _ in d.body.ran] == ["dance", "bow"]
    assert first["id"] != second["id"] and not c.state()["busy"]


def test_idle_relaxes_torque(robot, monkeypatch):
    monkeypatch.setattr(server, "IDLE_RELAX_S", 0.1)
    d, c = robot
    c.run("wave")
    time.sleep(0.6)
    assert d.body.relaxed >= 1 and not d.torque_maybe_on


def test_watchdog_check_waits_for_the_idle_delay(robot, monkeypatch):
    """The periodic 'torque left on' check must not unload right as a skill ends (Tony fell mid-sway)."""
    monkeypatch.setattr(server, "IDLE_RELAX_S", 0.8)
    monkeypatch.setattr(server, "WATCH_S", 0.0)  # check on every loop, so it would hit the end of the skill
    d, c = robot
    d.body.status = lambda: {"torque_on": 16, "temps": {1: 40}, "hottest": (1, "left ankle", 40), "battery_mv": 11500}
    time.sleep(3.2)  # the first periodic check comes 3 s after start
    before = d.body.relaxed
    c.run("wave")
    time.sleep(0.4)  # well inside the idle delay
    assert d.body.relaxed == before
    time.sleep(0.8)
    assert d.body.relaxed > before


def test_settles_before_going_limp_after_body_moves(robot, monkeypatch):
    """Going limp in a walk/turn end pose and starting the next step from it put Tony on his back."""
    monkeypatch.setattr(server, "IDLE_RELAX_S", 0.1)
    d, c = robot
    c.run("turn_left")
    time.sleep(0.6)
    assert d.body.ran == [("turn_left", 1), ("settle", 1)] and d.body.relaxed >= 1
    d.body.ran.clear()
    c.run("nod")  # head only: no settle
    c.run("settle")  # already a stand: no second settle
    time.sleep(0.6)
    assert d.body.ran == [("nod", 1), ("settle", 1)]


def test_no_settle_after_stop(robot, monkeypatch):
    monkeypatch.setattr(server, "IDLE_RELAX_S", 0.1)
    d, c = robot
    d.body.seconds = 2
    c.run("walk_forward", wait=False)
    time.sleep(0.1)
    c.stop()
    time.sleep(0.6)
    assert ("settle", 1) not in d.body.ran


def test_head_is_clamped(robot):
    d, c = robot
    assert c.head(100, -100) == {"pan": 60.0, "tilt": -40.0}
    pulses = d.body.board.heads[-1]
    assert pulses[server.PAN] == round(1466 + 60 * server.US_PER_DEG)


def test_state_and_no_camera(robot):
    d, c = robot
    st = c.state(fresh=True)
    assert st["battery_mv"] == 11500 and st["hottest"] == [1, "left ankle", 40] and st["camera"]["ok"] is False
    with pytest.raises(TonydError, match="camera"):
        c.snapshot()


def test_camera_lost_and_back_are_announced():
    from tonypi.tonyd.camera import Camera

    cam = Camera()
    d = Tonyd(_Body(), cam)
    events = []
    d.emit = lambda event, **kw: events.append((event, kw))
    cam._set_connected(True)   # opened fine at startup: no news
    cam._set_connected(True)
    cam._set_connected(False)  # unplugged
    cam._set_connected(False)  # still gone: said once
    cam._set_connected(True)   # back
    assert events == [("warn", {"text": "Camera disconnected.", "kind": "camera"}),
                      ("warn", {"text": "Camera reconnected.", "kind": "camera"})]


def test_camera_missing_at_startup_is_announced():
    from tonypi.tonyd.camera import Camera

    cam = Camera()
    d = Tonyd(_Body(), cam)
    events = []
    d.emit = lambda event, **kw: events.append(kw["text"])
    cam._set_connected(False)
    assert events == ["Camera disconnected."]
