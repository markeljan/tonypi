"""Check request settings without starting Codex, audio, or robot connections."""
import asyncio
import itertools

import pytest

from tonypi.voice import live


@pytest.mark.parametrize("resume", [False, True])
def test_brain_speed_settings_apply_on_start_and_resume(monkeypatch, resume):
    brain = object.__new__(live.LiveBrain)
    calls = []
    monkeypatch.setattr(brain, "_load_state", lambda: {"thread": "old", "tools_hash": live.TOOLS_HASH} if resume else {})
    monkeypatch.setattr(brain, "_save_state", lambda **kw: None)

    async def rpc(method, params, **kw):
        calls.append((method, params))
        return {"thread": {"id": "brain"}}

    monkeypatch.setattr(brain, "_rpc", rpc)
    assert asyncio.run(brain.setup_thread()) == "brain"
    method, params = calls[0]
    assert method == ("thread/resume" if resume else "thread/start")
    assert params["model"] == live.MODEL
    assert params["serviceTier"] == live.SERVICE_TIER
    assert params["config"]["model_reasoning_effort"] == "low"


@pytest.mark.parametrize("fork", [False, True])
@pytest.mark.parametrize("effort", [None, "high"])
def test_workers_use_fast_settings_and_preserve_effort_override(monkeypatch, fork, effort):
    brain = object.__new__(live.LiveBrain)
    brain.tid = "brain"
    brain.workers, brain.by_thread = {}, {}
    brain.task_ids = itertools.count(1)
    brain.convo = type("Convo", (), {"tail": lambda self, n: []})()
    calls = []

    async def rpc(method, params, **kw):
        calls.append((method, params))
        return {"turn": {"id": "turn"}} if method == "turn/start" else {"thread": {"id": "worker"}}

    monkeypatch.setattr(brain, "_rpc", rpc)
    args = {"title": "test", "brief": "test", "full_context": fork, "uses_body": False}
    if effort:
        args["effort"] = effort
    assert asyncio.run(brain._start_task(args)).startswith("started t1")
    method, params = calls[0]
    assert method == ("thread/fork" if fork else "thread/start")
    assert params["model"] == live.MODEL
    assert params["serviceTier"] == live.SERVICE_TIER
    assert params["config"]["service_tier"] == live.SERVICE_TIER
    assert params["config"]["model_reasoning_effort"] == (effort or "low")
    assert calls[1][1]["effort"] == (effort or "low")
