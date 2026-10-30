"""Check L and worker request settings (and L's rotation) without starting Codex, audio, or robot connections."""
import asyncio
import time

import pytest

from tonypi.voice import live


@pytest.fixture(autouse=True)
def memory(monkeypatch):
    store = type("S", (), {"memory_snapshot": lambda self: "MEM", "journal": lambda self, *a, **k: None})()
    monkeypatch.setattr(live, "_brain_store", lambda: store)


def _brain(monkeypatch, state):
    brain = object.__new__(live.LiveBrain)
    calls = []
    monkeypatch.setattr(brain, "_load_state", lambda: state)
    monkeypatch.setattr(brain, "_save_state", lambda **kw: state.update(kw))

    async def rpc(method, params, **kw):
        calls.append((method, params))
        return {"thread": {"id": "brain"}}

    monkeypatch.setattr(brain, "_rpc", rpc)
    return brain, calls


@pytest.mark.parametrize("resume", [False, True])
def test_brain_speed_settings_apply_on_start_and_resume(monkeypatch, resume):
    state = {"thread": "old", "tools_hash": live.TOOLS_HASH, "created_t": time.time() - 3600} if resume else {}
    brain, calls = _brain(monkeypatch, state)
    assert asyncio.run(brain.setup_thread()) == "brain"
    method, params = calls[0]
    assert method == ("thread/resume" if resume else "thread/start")
    assert params["model"] == live.MODEL == "gpt-6.1-terra"
    assert params["serviceTier"] == live.SERVICE_TIER
    assert params["config"]["model_reasoning_effort"] == "low"
    assert params["developerInstructions"].endswith("## Memory snapshot\nMEM")


@pytest.mark.parametrize("state", [
    {"created_t": time.time() - 21 * 3600},  # too old
    {"created": "2026-01-01T00:00:00"},  # old state files only have the date string
    {"created_t": time.time(), "l_input_tokens": 120_000},  # context too big
    {"created_t": time.time(), "tools_hash": "changed"},
])
def test_brain_thread_rotates_with_a_continuity_note(monkeypatch, state):
    state = {"thread": "old", "tools_hash": live.TOOLS_HASH, **state}
    brain, calls = _brain(monkeypatch, state)
    assert asyncio.run(brain.setup_thread()) == "brain"
    assert [m for m, _ in calls] == ["thread/start", "thread/inject_items"]
    note = calls[1][1]["items"][0]["content"][0]["text"]
    assert "old" in note and str(live.BRAIN_DIR) in note and "tony recall" in note
    assert state["thread"] == "brain" and state["l_input_tokens"] == 0 and state["previous_thread"] == "old"


@pytest.mark.parametrize("fork", [False, True])
@pytest.mark.parametrize("effort", [None, "high"])
def test_workers_use_fast_settings_and_preserve_effort_override(monkeypatch, fork, effort):
    brain = object.__new__(live.LiveBrain)
    brain.tid = "brain"
    brain.workers, brain.by_thread = {}, {}
    brain.last_user_speech_t = brain.last_input_t = 0.0
    brain.convo = type("Convo", (), {"tail": lambda self, n: []})()
    monkeypatch.setattr(brain, "_next_task_id", lambda: "1030-t1")
    calls = []

    async def rpc(method, params, **kw):
        calls.append((method, params))
        return {"turn": {"id": "turn"}} if method == "turn/start" else {"thread": {"id": "worker"}}

    monkeypatch.setattr(brain, "_rpc", rpc)
    args = {"title": "test", "brief": "test", "full_context": fork, "uses_body": False}
    if effort:
        args["effort"] = effort
    assert asyncio.run(brain._start_task(args)).startswith("started 1030-t1")
    method, params = calls[0]
    assert method == ("thread/fork" if fork else "thread/start")
    assert params["model"] == live.MODEL
    assert params["ephemeral"] is False
    assert params["serviceTier"] == live.SERVICE_TIER
    assert params["config"]["service_tier"] == live.SERVICE_TIER
    assert params["config"]["model_reasoning_effort"] == (effort or "low")
    assert calls[1][1]["effort"] == (effort or "low")
    assert calls[1][1]["outputSchema"] is live.WORKER_SCHEMA
    assert brain.workers["1030-t1"]["source"] == "self"  # nobody spoke recently
