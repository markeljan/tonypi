"""Fairness filter: Hermes sees and uses only defaults and its own *_hermes moves; saves only *_hermes."""

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from tonypi.hermes import robot_mcp


class FakeTonyd:
    def __init__(self) -> None:
        self.calls = []

    def call(self, op, **kw):
        self.calls.append((op, kw))
        if op == "skills":
            return {"details": {"wave": {"source": "default"}, "settle": {"source": "builtin"},
                                "turn_left_pi": {"source": "user"}, "try_turn_own": {"source": "user"},
                                "high_step_hermes": {"source": "user"}}}
        if op == "motions":
            return {"motions": [{"name": "turn_left", "source": "default"}, {"name": "turn_left_pi", "source": "user"},
                                {"name": "step_hermes", "source": "user"}]}
        if op == "run":
            return {"job": {"skill": kw["skill"], "state": "done"}}
        return {"ok": True}


@pytest.fixture
def tonyd(monkeypatch):
    fake = FakeTonyd()
    monkeypatch.setattr(robot_mcp.shared, "tonyd", fake)
    return fake


def test_listings_hide_other_harnesses(tonyd):
    assert set(robot_mcp.list_skills()) == {"wave", "settle", "high_step_hermes"}
    assert [m["name"] for m in robot_mcp.list_motions()] == ["turn_left", "step_hermes"]


def test_using_another_harness_move_is_refused(tonyd):
    with pytest.raises(ToolError):
        robot_mcp.run_skill("turn_left_pi")
    with pytest.raises(ToolError):
        robot_mcp.get_motion("turn_left_pi")
    assert robot_mcp.run_skill("high_step_hermes")["state"] == "done"
    assert robot_mcp.run_skill("wave")["state"] == "done"


def test_saves_and_deletes_only_own_names(tonyd):
    for bad in (lambda: robot_mcp.save_motion("turn_left", []), lambda: robot_mcp.save_skill("walk_forward", ["x"]),
                lambda: robot_mcp.delete_skill("turn_left_pi"), lambda: robot_mcp.delete_motion("turn_left_pi"),
                lambda: robot_mcp.save_skill("x_hermes", ["*turn_left_pi"])):
        with pytest.raises(ToolError):
            bad()
    robot_mcp.save_skill("x_hermes", ["*step_hermes", "turn_left"])
    assert ("skill_save", {"name": "x_hermes", "motions": ["*step_hermes", "turn_left"], "description": ""}) in tonyd.calls
