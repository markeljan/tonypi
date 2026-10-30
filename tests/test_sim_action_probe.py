"""Offline geometry and replay input checks; never use robot hardware."""
import importlib.util
import json
import math
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location("sim_action_probe", Path(__file__).parents[1] / "tools/sim_action_probe.py")
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


def test_box_bottom_accounts_for_tilted_corners():
    assert probe.box_bottom([0, 0, .4], [1, 0, 0, 0, 1, 0, 0, 0, 1], [.1, .2, .03]) == pytest.approx(.37)
    # A quarter turn puts the long x half-extent vertically, rather than the box's thin z extent.
    assert probe.box_bottom([0, 0, .4], [[0, 0, 1], [0, 1, 0], [-1, 0, 0]], [.1, .2, .03]) == pytest.approx(.3)
    c = math.sqrt(.5)
    assert probe.box_bottom([0, 0, .4], [c, 0, c, 0, 1, 0, -c, 0, c], [.1, .2, .03]) == pytest.approx(.4 - c * .13)


def test_phase_boundaries_use_completed_rounded_steps():
    ends = probe.phase_ends([{"ms": 5, "pos": {}}, {"ms": 8, "pos": {"1": 500}}], .004)
    assert ends == [2, 4]
    assert [probe.phase_for_step(n, ends) for n in range(6)] == [0, 0, 0, 1, 1, 2]


@pytest.mark.parametrize("ms", [0, -1, True, float("nan"), "100"])
def test_invalid_frame_duration(ms):
    with pytest.raises(ValueError, match="ms"):
        probe.phase_ends([{"ms": ms, "pos": {}}], .004)


def test_load_request_and_sibling_reference(tmp_path):
    request = {"plan": {"raw_frames": [{"ms": 100, "pos": {"1": 500}}]}}
    (tmp_path / "request.json").write_text(json.dumps(request))
    (tmp_path / "physics.json").write_text('{"max_tilt_deg": 12}')
    for path in (tmp_path, tmp_path / "request.json", tmp_path / "physics.json"):
        assert probe.load_input(path) == (request, {"max_tilt_deg": 12})
    (tmp_path / "physics.json").unlink()
    assert probe.load_input(tmp_path) == (request, None)


def test_load_rejects_unrelated_input_and_invalid_plan(tmp_path):
    unrelated = tmp_path / "other.json"
    unrelated.write_text("{}")
    with pytest.raises(ValueError, match="input must"):
        probe.load_input(unrelated)
    (tmp_path / "request.json").write_text('{"plan": {"raw_frames": []}}')
    with pytest.raises(ValueError, match="nonempty"):
        probe.load_input(tmp_path)


def test_probe_restores_step_on_engine_failure(monkeypatch):
    import mujoco
    from tonypi.simulation import engine

    original = mujoco.mj_step

    def fail(request, directory):
        assert request["render"] is False
        assert mujoco.mj_step is not original
        raise RuntimeError("failed replay")

    monkeypatch.setattr(engine, "run", fail)
    request = {"render": True}
    with pytest.raises(RuntimeError, match="failed replay"):
        probe.probe(request)
    assert mujoco.mj_step is original
    assert request == {"render": True}
