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
        from tonypi.tonyd.body import DEFAULT_SKILLS
        from tonypi.tonyd.studio import Studio

        self.board, self.cancelled, self.asleep = _Board(), threading.Event(), False
        self.ran, self.relaxed, self.seconds = [], 0, seconds
        self.studio = Studio(Path(tempfile.mkdtemp()), default_skills=DEFAULT_SKILLS)
        self.max_tilt, self.torqued = 3.0, []

    def play_frames(self, frames, say):
        self.ran.append(("_play", len(frames)))
        return not self.cancelled.wait(self.seconds)

    def joints(self):
        return {"raw": {i: 500 for i in range(1, 17)}, "pos": {i: 500 for i in range(1, 17)}}

    def torque(self, ids, on):
        self.torqued.append((list(ids), on))

    def reload_calibration(self):
        return self.studio.calibration()

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


# ---- brains writing their own moves ---------------------------------------------------------------------------

STEP = [{"ms": 300, "pos": {"4": 620, "12": 380}}, {"ms": 300, "pos": {"4": 600, "12": 400}}]


def test_play_frames_runs_as_a_guarded_job_and_settles(robot, monkeypatch):
    monkeypatch.setattr(server, "IDLE_RELAX_S", 0.1)
    d, c = robot
    job = c.play(STEP)
    assert job["skill"] == "_play" and job["state"] == "done" and job["max_tilt_deg"] == 3.0
    time.sleep(0.6)
    assert d.body.ran == [("_play", 2), ("settle", 1)] and d.body.relaxed >= 1


def test_pose_hold_keeps_torque_and_skips_settle(robot, monkeypatch):
    monkeypatch.setattr(server, "IDLE_RELAX_S", 0.1)
    d, c = robot
    c.pose({"8": 900}, ms=200, hold_s=0.8)
    relaxed = d.body.relaxed
    time.sleep(0.4)
    assert d.body.relaxed == relaxed and ("settle", 1) not in d.body.ran  # still holding
    time.sleep(0.8)
    assert d.body.relaxed > relaxed and ("settle", 1) not in d.body.ran


def test_bad_frames_are_refused(robot):
    _, c = robot
    for bad in ([], [{"ms": 5, "pos": {"1": 500}}], [{"ms": 300, "pos": {"17": 500}}],
                [{"ms": 300, "pos": {"1": 1200}}], [{"ms": 300}]):
        with pytest.raises(TonydError):
            c.play(bad)


def test_user_motion_and_skill_round_trip(robot):
    d, c = robot
    r = c.call("motion_save", name="my_step", frames=STEP, description="test step")
    assert r["frames"] == 2 and not r["overrides_default"]
    assert any(m["name"] == "my_step" and m["source"] == "user" for m in c.call("motions")["motions"])
    assert c.call("motion_get", name="my_step")["frames"] == STEP
    with pytest.raises(TonydError, match="no motion"):
        c.call("skill_save", name="shuffle", motions=["nope"])
    c.call("skill_save", name="shuffle", motions=["*my_step"], description="shuffle forward")
    assert c.call("skills")["details"]["shuffle"]["source"] == "user"
    assert c.run("shuffle", 2)["state"] == "done" and ("shuffle", 2) in d.body.ran
    # overriding a default, then restoring it
    assert c.call("skill_save", name="turn_left", motions=["*my_step"])["overrides_default"]
    assert c.call("skills")["details"]["turn_left"]["motions"] == ["*my_step"]
    c.call("skill_delete", name="turn_left")
    assert c.call("skills")["details"]["turn_left"]["motions"] == ["*turn_left"]


def test_calibration_trim_and_limits():
    from tonypi.tonyd.studio import Studio, StudioError

    st = Studio(Path(tempfile.mkdtemp()))
    cal = st.set_calibration(trim={"4": 12, "12": -8}, limits={"8": [200, 800]})
    assert st.to_servo(cal, {4: 600, 12: 400, 8: 950, 1: 1000}) == {4: 612, 12: 392, 8: 800, 1: 1000}
    cal = st.set_calibration(trim={"4": 0}, limits={"8": None})
    assert cal == {"trim": {12: -8}, "limits": {}}
    with pytest.raises(StudioError):
        st.set_calibration(trim={"4": 500})


def test_body_plays_user_skills_with_calibration():
    """The real Body: a user skill's motions, repeated by count, reach the board with the trim applied."""
    from tonypi.motion.library import Player
    from tonypi.tonyd.body import DEFAULT_SKILLS, Body
    from tonypi.tonyd.studio import Studio

    class Board:
        imu, battery_mv = _Imu(), 11500

        def __init__(self):
            self.moves = []

        def bus_read(self, sid, cmd, timeout=0.05, retries=2):
            return 40

        def move(self, ms, targets):
            self.moves.append(dict(targets))

    body = Body.__new__(Body)
    body.board, body.cancelled, body.asleep, body._last_dance = Board(), threading.Event(), False, None
    body.player = Player(body.board)
    body.studio = Studio(Path(tempfile.mkdtemp()), default_skills=DEFAULT_SKILLS)
    body.studio.save_motion("tiny_step", [{"ms": 20, "pos": {"4": 600}}])
    body.studio.save_skill("tiny_walk", ["*tiny_step"])
    body.studio.set_calibration(trim={"4": 10})
    body.reload_calibration()
    assert body.run("tiny_walk", 3, lambda s: None)
    assert body.board.moves == [{4: 610}] * 3
