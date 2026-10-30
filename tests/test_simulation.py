"""Simulation tests never submit to the live robot: every hardware client below is a fake."""

import copy
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest

from tonypi.simulation import catalog, service


class Robot:
    def __init__(self):
        self.cal = {"trim": {}, "limits": {}}
        self.st = {"stop_latched": False, "busy": False, "lying": False, "tilt_deg": 0,
                   "battery_mv": 11500, "hottest": [1, "ankle", 35], "tether": {"on": False}}
        self.raw = {i: 500 for i in range(1, 17)}
        self.frames = {n: [{"ms": 100, "pos": {1: p}}] for n, p in (("start", 490), ("step", 510), ("end", 500))}
        self.moves, self.cancelled = [], False

    def call(self, op, **kwargs):
        if op == "calibration":
            return {"calibration": copy.deepcopy(self.cal)}
        if op == "motion_get":
            return {"frames": copy.deepcopy(self.frames[kwargs["name"]])}
        if op == "skills":
            return {"details": {"walk_forward": {"motions": ["start", "*step", "end"]}, "get_up": {}}}
        if op == "motions":
            return {"motions": [{"name": n, "source": "fake"} for n in self.frames]}
        raise AssertionError(f"unexpected robot op {op}")

    def state(self, **kwargs):
        return copy.deepcopy(self.st)

    def joints(self):
        return {"raw": self.raw.copy()}

    def _move(self, kind, payload, kwargs):
        self.moves.append((kind, payload, kwargs))
        return {"id": 1, "state": "cancelled" if self.cancelled else "done", "max_tilt_deg": 2,
                "end_tilt_deg": 1, "lying_after": False, "yaw_deg": 0, "seconds": 1}

    def run(self, skill, count=1, **kwargs):
        return self._move("run", (skill, count), kwargs)

    def play(self, frames, **kwargs):
        return self._move("play", frames, kwargs)


def action(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE ActionGroup ("Index" INTEGER, Time INTEGER, Servo1 INTEGER, Servo2 INTEGER, Servo17 INTEGER)')
        db.executemany("INSERT INTO ActionGroup VALUES (?,?,?,?,?)", rows)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("TONYPI_SIM_DIR", str(tmp_path))
    repo = catalog.repository()
    action(repo / "action/wave.d6a", [(2, 100, 530, None, 500), (1, 200, 510, 490, 500)])
    monkeypatch.setattr(catalog, "verify", lambda p: catalog.REVISION)
    return repo, Robot()


def test_d6a_order_partial_channels_readonly(setup):
    repo, _ = setup
    fs, ignored = catalog.d6a(repo / "action/wave.d6a")
    assert fs == [{"ms": 200, "pos": {1: 510, 2: 490}}, {"ms": 100, "pos": {1: 530}}]
    assert ignored == [17]
    assert not (repo / "action/wave.d6a-journal").exists()


@pytest.mark.parametrize("frames", [[], [{"ms": 0, "pos": {1: 500}}], [{"ms": 20, "pos": {17: 500}}],
                                         [{"ms": 20, "pos": {1: 1001}}], [{"ms": 20, "pos": {1: True}}],
                                         [{"ms": 10000, "pos": {1: 500}}] * 13])
def test_reject_bad_frames(frames):
    with pytest.raises(ValueError):
        catalog.frames_checked(frames)


def test_calibration_raw_bridge_and_clipping(setup):
    repo, robot = setup
    robot.cal = {"trim": {1: 10}, "limits": {1: [0, 520]}}
    p = catalog.resolve(repo, robot, "upstream:wave")
    assert p["raw_frames"][0]["pos"][1] == 510
    assert p["hardware_frames"][0]["pos"][1] == 500  # tonyd adds trim once
    assert p["raw_frames"][1]["pos"][1] == 520
    assert p["soft_limit_clips"] == [{"frame": 1, "servo": 1, "requested": 530, "actual": 520}]
    q = catalog.resolve(repo, robot, frames=[{"ms": 100, "pos": {1: 500}}])
    assert q["raw_frames"][0]["pos"][1] == 510
    assert q["hardware_frames"][0]["pos"][1] == 500


def test_skill_repeat_and_dynamic_skill(setup):
    repo, robot = setup
    p = catalog.resolve(repo, robot, "skill:walk_forward", count=3)
    assert [f["pos"][1] for f in p["raw_frames"]] == [490, 510, 510, 510, 500]
    with pytest.raises(ValueError, match="scripted/dynamic"):
        catalog.resolve(repo, robot, "skill:get_up")
    assert not robot.moves


@pytest.mark.parametrize("motion", ["motion:step", "skill:walk_forward"])
def test_local_factory_head_channels_match_body_parser(setup, motion):
    repo, robot = setup
    robot.frames["step"][0]["pos"].update({17: 500, 18: 500})
    p = catalog.resolve(repo, robot, motion)
    assert p["ignored_channels"] == [17, 18]
    assert all(set(f["pos"]) <= set(range(1, 17)) for f in p["raw_frames"])
    assert not robot.moves


@pytest.mark.parametrize("name", ["upstream:../wave", "upstream:/tmp/wave", "wave", "unknown:wave"])
def test_names_cannot_escape_catalog(setup, name):
    with pytest.raises(ValueError):
        catalog.resolve(*setup, name)


def test_catalog_lists_unsupported_without_moving(setup):
    c = catalog.catalog(*setup)
    assert len(c["upstream"]) == 1
    get_up = next(r for r in c["local"] if r["id"] == "skill:get_up")
    assert not get_up["simulation_supported"]
    assert not setup[1].moves


@pytest.fixture
def simulated(setup, monkeypatch):
    repo, robot = setup

    def worker(cmd, **kwargs):
        directory = Path(cmd[-1])
        request = json.loads((directory / "request.json").read_text())
        assert request["plan"]["raw_frames"]
        assert kwargs["env"]["MUJOCO_GL"] == "osmesa"
        assert kwargs["timeout"] == 90
        (directory / "physics.json").write_text(json.dumps({
            "simulation_success": True, "model_joint_clips": [], "trace": [{"t_s": 1}],
            "initial_raw": robot.raw, "hardware_validated": False, "model_only": True}))
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(service.subprocess, "run", worker)
    result = service.simulate(robot, frames=[{"ms": 100, "pos": {1: 500}}], repo=repo)
    assert not robot.moves
    assert result["hardware_candidate"] and not result["hardware_executed"]
    return repo, robot, result


@pytest.fixture
def real_lab(tmp_path, monkeypatch):
    from tonypi.learn import runner

    original = runner.Lab
    monkeypatch.setattr(runner, "Lab", lambda **kwargs: original(**kwargs, root=tmp_path / "learning",
                                                               use_phone=False, sleep=lambda s: None))


@pytest.mark.parametrize("state,reason", [({"stop_latched": True}, "stop latch"), ({"busy": True}, "idle upright"),
                                          ({"lying": True}, "idle upright"), ({"tilt_deg": 11}, "idle upright"),
                                          ({"battery_mv": 10800}, "battery"), ({"battery_mv": None}, "battery"),
                                          ({"hottest": [1, "ankle", 70]}, "temperature"),
                                          ({"tether": {"on": True}}, "tethered")])
def test_execute_refuses_without_moving(simulated, state, reason):
    _, robot, report = simulated
    robot.st.update(state)
    with pytest.raises(ValueError, match=reason):
        service.execute(robot, report["id"])
    assert robot.moves == []
    assert robot.st.get("stop_latched") == state.get("stop_latched", False)


def test_execute_checks_pose_and_changed_calibration(simulated):
    _, robot, report = simulated
    robot.raw[1] += 21
    with pytest.raises(ValueError, match="starting pose"):
        service.execute(robot, report["id"])
    robot.raw[1] -= 21
    robot.cal["trim"][1] = 1
    with pytest.raises(ValueError, match="changed since"):
        service.execute(robot, report["id"])
    assert not robot.moves


def test_execute_uses_guarded_trial_and_stable_finish(simulated, real_lab):
    _, robot, report = simulated
    result = service.execute(robot, report["id"])
    assert result["hardware_validated"]
    assert robot.moves[0][0] == "play"
    assert robot.moves[0][2]["guard_tilt_deg"] == 30
    assert robot.moves[-1][1] == ("settle", 1)


def test_cancelled_trial_has_no_cleanup_move(simulated, real_lab):
    _, robot, report = simulated
    robot.cancelled = True
    result = service.execute(robot, report["id"])
    assert not result["hardware_validated"]
    assert len(robot.moves) == 1
    assert result["physical_trial"]["trials"][0]["verdict"] == "cancelled"


def test_submission_rechecks_tether(setup):
    repo, robot = setup
    plan = catalog.resolve(repo, robot, frames=[{"ms": 100, "pos": {1: 500}}])
    guarded = service.TrialClient(robot, plan, robot.raw.copy())
    guarded.check()
    robot.st["tether"]["on"] = True
    with pytest.raises(ValueError, match="tethered"):
        guarded.play(plan["hardware_frames"])
    assert not robot.moves


def test_native_skill_preserves_name_and_refuses_tether(setup):
    repo, robot = setup
    plan = catalog.resolve(repo, robot, "skill:walk_forward", count=3)
    robot.st["tether"]["on"] = True
    guarded = service.TrialClient(robot, plan, robot.raw.copy())
    with pytest.raises(ValueError, match="tethered"):
        guarded.run("walk_forward", 3, guard_tilt_deg=30)
    assert not robot.moves
    robot.st["tether"]["on"] = False
    guarded.run("walk_forward", 3, guard_tilt_deg=30)
    assert robot.moves[0][1] == ("walk_forward", 3)


def test_new_physical_tool_hidden_in_no_body_sessions():
    from tonypi.voice.live import BODY_TOOLS

    assert "simulation_execute" in BODY_TOOLS


def test_actual_headless_model(tmp_path, monkeypatch):
    pytest.importorskip("mujoco")
    repo = catalog.repository()
    if not (repo / "model/scene.xml").exists():
        pytest.skip("pinned upstream assets not installed")
    catalog.verify(repo)
    zero, _ = catalog.mapping(repo)
    monkeypatch.setenv("TONYPI_SIM_DIR", str(tmp_path))
    r = service.simulate(frames=[{"ms": 1000, "pos": zero}], render=False, repo=repo)
    assert r["simulation_success"] and r["finite"]
    assert r["end_tilt_deg"] < 5
    assert r["model_joint_clip_count"] == 0
    assert r["model_only"] and not r["hardware_candidate"] and not r["hardware_validated"]
    assert r["physics_seconds"] == pytest.approx(2)
