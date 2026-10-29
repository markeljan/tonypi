"""pi front end: stop never reaches the model; a new command while pi works becomes a steer (tool running) or an
abort + prompt (model generating). Fake pi and fake tonyd, no hardware, no models."""

from __future__ import annotations

import threading
import time

import pytest

from tonypi.pi_front import daemon


class FakePi:
    def __init__(self) -> None:
        self.sent: list[tuple[str, dict]] = []
        self.lock = threading.Lock()

    def send(self, type_: str, timeout: float = 30, **kw) -> dict:
        with self.lock:
            self.sent.append((type_, kw))
        return {}

    def send_async(self, type_: str, **kw) -> None:
        self.send(type_, **kw)

    def types(self) -> list[str]:
        with self.lock:
            return [t for t, _ in self.sent]


class FakeTonyd:
    def __init__(self) -> None:
        self.stops = 0

    def stop(self) -> None:
        self.stops += 1


@pytest.fixture(autouse=True)
def _log(tmp_path, monkeypatch):
    monkeypatch.setattr(daemon, "LOG", tmp_path / "pi-front.jsonl")


def front() -> daemon.Front:
    f = daemon.Front.__new__(daemon.Front)
    f.speech = daemon.Speech(None)
    f.tonyd, f.pi = FakeTonyd(), FakePi()
    f.ids = iter(range(1, 100))
    f.cmd, f.running, f.tools_running, f.restarting = None, False, 0, False
    f.last_settled = time.monotonic()
    f.lock = threading.RLock()
    f.clients, f.clients_lock = set(), threading.Lock()
    f.listener = None
    return f


def wait_for(cond, s: float = 2.0) -> None:
    t = time.monotonic() + s
    while not cond() and time.monotonic() < t:
        time.sleep(0.01)
    assert cond()


def test_idle_command_is_a_prompt():
    f = front()
    f.submit("wave", "terminal")
    assert f.pi.sent == [("prompt", {"message": "wave", "streamingBehavior": "steer"})]
    assert f.running


def test_stop_goes_to_tonyd_not_the_model():
    f = front()
    f.submit("walk forward 3 steps", "terminal")
    f._on_pi({"type": "agent_start"})
    f._on_pi({"type": "tool_execution_start", "toolName": "run_skill", "args": {"skill": "walk_forward", "count": 3}})
    f.submit("stop", "voice")
    assert f.tonyd.stops == 1
    wait_for(lambda: "abort" in f.pi.types())
    assert not any(t in ("prompt", "steer") and kw.get("message") == "stop" for t, kw in f.pi.sent)


def test_new_command_during_a_walk_steers():
    f = front()
    f.submit("walk forward 3 steps, then turn left", "terminal")
    f._on_pi({"type": "agent_start"})
    f._on_pi({"type": "tool_execution_start", "toolName": "run_skill", "args": {"skill": "walk_forward", "count": 3}})
    f.submit("actually, turn right instead", "terminal")
    wait_for(lambda: "steer" in f.pi.types())
    assert ("steer", {"message": "actually, turn right instead"}) in f.pi.sent
    assert "abort" not in f.pi.types()
    assert f.cmd.how == "steer"


def test_new_command_while_generating_aborts_and_prompts():
    f = front()
    f.submit("what do you see?", "terminal")
    f._on_pi({"type": "agent_start"})
    f.submit("dance instead", "terminal")
    wait_for(lambda: f.pi.types()[-1:] == ["prompt"] and len(f.pi.sent) == 4)
    assert f.pi.types() == ["prompt", "clear_queue", "abort", "prompt"]
    assert f.cmd.text == "dance instead"


def test_aborted_run_settling_does_not_finish_the_new_command():
    f = front()
    f.submit("what do you see?", "terminal")
    f._on_pi({"type": "agent_start"})
    f.restarting = True  # as _restart sets it while pi aborts the old run
    new = f.cmd = daemon.Command(9, "dance", "terminal")
    f._on_pi({"type": "agent_settled", "aborted": True})
    assert f.cmd is new
    f.restarting = False
    f._on_pi({"type": "agent_settled", "aborted": False})
    assert f.cmd is None and not f.running


def test_say_tool_and_final_reply_are_spoken():
    f = front()
    spoken: list[str] = []
    f.speech.say = spoken.append
    f.submit("go to the door", "terminal")
    f._on_pi({"type": "tool_execution_start", "toolName": "say", "args": {"text": "On my way."}})
    f._on_pi({"type": "message_end", "message": {"role": "assistant", "stopReason": "stop", "provider": "local",
                                                 "model": "lfm3", "content": [{"type": "text", "text": "I'm there."}]}})
    f._on_pi({"type": "message_end", "message": {"role": "assistant", "stopReason": "aborted",
                                                 "content": [{"type": "text", "text": "half a reply"}]}})
    assert spoken == ["On my way.", "I'm there."]
