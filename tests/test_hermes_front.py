"""Hermes front end routing with fakes: stop never reaches Hermes, commands during a run steer it, late steers replay,
deep tasks persist until task_done, and every task is logged."""

import json
import queue
import time

import pytest

from tonypi.hermes import front as front_mod
from tonypi.hermes.front import Front, tier_for


class FakeTonyd:
    def __init__(self) -> None:
        self.stops = 0
        self.calls: list[tuple] = []
        self.user_motions: list[str] = []

    def stop(self) -> None:
        self.stops += 1

    def state(self, fresh: bool = False) -> dict:
        return {"battery_mv": 12000}

    def call(self, op, **kw):
        self.calls.append((op, kw))
        if op == "skills":
            return {"details": {}}
        if op == "motions":
            return {"motions": [{"name": n, "source": "user"} for n in self.user_motions]}
        return {}


class FakeHermes:
    """A run lasts until the test releases it; steer() is accepted while it runs unless `late` is set."""

    def __init__(self, late: bool = False) -> None:
        self.started: list[tuple[str, str, str]] = []
        self.steered: list[str] = []
        self.stopped: list[str] = []
        self.late = late
        self.release: queue.Queue = queue.Queue()

    def start(self, text, session_id, model="", provider="", effort=""):
        self.started.append((text, model, effort))
        return f"run{len(self.started)}"

    def events(self, run_id):
        yield {"event": "tool.started", "tool": "mcp__tonypi__run_skill", "preview": "walk_forward"}
        kind, on_end = self.release.get(timeout=5)
        if on_end:
            on_end()
        pending = "\n".join(self.steered) if self.late else ""
        yield {"event": kind, "output": f"done {run_id}", "pending_steer": pending,
               "usage": {"input_tokens": 100, "output_tokens": 5}}

    def finish(self, kind="run.completed", on_end=None):
        self.release.put((kind, on_end))

    def steer(self, run_id, text):
        self.steered.append(text)
        return True

    def stop(self, run_id):
        self.stopped.append(run_id)
        self.release.put(("run.cancelled", None))


@pytest.fixture(autouse=True)
def state_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(front_mod, "STATE", tmp_path)
    monkeypatch.setattr(front_mod, "TASK_LOG", tmp_path / "tasks.jsonl")
    monkeypatch.setattr(front_mod, "TIER", "fast")
    return tmp_path


def make(hermes):
    said: list[str] = []
    f = Front(hermes, tonyd=FakeTonyd(), speak=said.append, stop_speaking=lambda: None)
    events: list[dict] = []
    f.listeners.append(events.append)
    return f, said, events


def wait(pred, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.01)
    return False


def test_stop_goes_to_the_body_not_the_llm():
    h = FakeHermes()
    f, said, events = make(h)
    f.submit("stop")
    assert f.tonyd.stops == 1 and h.started == []


def test_stop_during_a_run_kills_it():
    h = FakeHermes()
    f, said, events = make(h)
    f.submit("walk forward 3 steps")
    assert wait(lambda: f.run_id not in (None, "starting"))
    f.submit("Stop!")
    assert f.tonyd.stops == 1 and h.stopped == ["run1"]
    assert wait(lambda: f.run_id is None)
    assert not any("done run1" in s for s in said)


def test_new_command_steers_the_running_turn():
    h = FakeHermes()
    f, said, events = make(h)
    f.submit("walk forward 3 steps, then turn left")
    assert wait(lambda: f.run_id not in (None, "starting"))
    f.submit("actually, turn right instead")
    assert h.steered == ["actually, turn right instead"] and f.tonyd.stops == 1  # the walk is cut at once
    assert len(h.started) == 1
    h.finish()
    assert wait(lambda: f.run_id is None)
    assert wait(lambda: sum(e["type"] == "done" for e in events) == 2)  # both submissions finish with the run


def test_late_steer_becomes_the_next_turn():
    h = FakeHermes(late=True)
    f, said, events = make(h)
    f.submit("wave")
    assert wait(lambda: f.run_id not in (None, "starting"))
    f.submit("and bow")
    h.finish()
    assert wait(lambda: len(h.started) == 2)
    assert h.started[1][0] == "and bow"
    h.late = False
    h.finish()
    assert wait(lambda: f.run_id is None)


def test_tiers(monkeypatch):
    monkeypatch.setattr(front_mod, "TIER", "auto")
    for fast in ("wave", "what do you see?", "turn left 2 steps", "how's your battery and how hot are you?"):
        assert tier_for(fast) == "fast", fast
    for deep in ("walk to the chair", "find my shoes", "do a jumping jack", "wave with both hands, then bow",
                 "learn to wave better", "where's the red cup?", "walk forward 3 steps, then turn left"):
        assert tier_for(deep) == "deep", deep
    monkeypatch.setattr(front_mod, "TIER", "deep")
    assert tier_for("wave") == "deep"


def test_fast_tier_sends_model_and_effort():
    h = FakeHermes()
    f, said, events = make(h)
    f.submit("wave")
    assert wait(lambda: h.started)
    model, effort = front_mod.TIERS["fast"]
    assert h.started[0] == ("wave", model, effort)
    h.finish()
    assert wait(lambda: said == ["done run1"])


def report(state_dir, success=True):
    return lambda: (state_dir / "task_report.json").write_text(
        json.dumps({"t": time.time(), "success": success, "summary": "I'm at the chair.", "how": "scan, turn, walk"}))


def test_deep_task_persists_until_task_done_and_is_logged(monkeypatch, state_dir):
    monkeypatch.setattr(front_mod, "TIER", "deep")
    h = FakeHermes()
    f, said, events = make(h)
    f.submit("walk to the chair")
    assert wait(lambda: len(h.started) == 1)
    f.tonyd.user_motions.append("jumping_jack_hermes")
    h.finish()  # ended without task_done: the harness asks it to keep going
    assert wait(lambda: len(h.started) == 2)
    assert "not reported done" in h.started[1][0] and h.started[1][1] == front_mod.TIERS["deep"][0]
    h.finish(on_end=report(state_dir))
    assert wait(lambda: f.run_id is None)
    assert len(h.started) == 2
    assert wait(lambda: (state_dir / "tasks.jsonl").exists())
    rec = json.loads((state_dir / "tasks.jsonl").read_text().splitlines()[-1])
    assert rec["success"] is True and rec["retries"] == 1 and rec["turns"] == 2
    assert rec["tokens_in"] == 200 and rec["created"] == ["motion:jumping_jack_hermes"]
    assert rec["first_action_s"] is not None and rec["how"] == "scan, turn, walk"


def test_deep_task_gives_up_at_the_cap(monkeypatch, state_dir):
    monkeypatch.setattr(front_mod, "TIER", "deep")
    monkeypatch.setattr(front_mod, "MAX_CONTINUES", 2)
    h = FakeHermes()
    f, said, events = make(h)
    f.submit("find my shoes")
    for n in range(3):
        assert wait(lambda: len(h.started) == n + 1)
        h.finish()
    assert wait(lambda: f.run_id is None)
    assert len(h.started) == 3 and any("out of time" in s for s in said)


def test_tether_reset_at_task_start(monkeypatch):
    monkeypatch.setattr(front_mod, "TETHER_RESET", True)
    h = FakeHermes()
    f, said, events = make(h)
    f.submit("wave")
    assert wait(lambda: h.started)
    assert ("tether", {"reset": True}) in f.tonyd.calls
    h.finish()


def test_status_over_the_socket_names_the_harness(tmp_path):
    import socket as so

    from tonypi.hermes.peripherals import SocketPeripheral

    class T(FakeTonyd):
        def state(self, fresh=False):
            return {"battery_mv": 12000, "busy": False}  # tonyd's own "busy" must not clash

    f = Front(FakeHermes(), tonyd=T())
    path = str(tmp_path / "a.sock")
    SocketPeripheral(f, path).start()
    s = so.socket(so.AF_UNIX)
    s.settimeout(3)
    s.connect(path)
    s.sendall(b'{"text": "/status"}\n')
    ev = json.loads(s.makefile().readline())
    assert ev["type"] == "status" and ev["harness"] == "hermes" and ev["busy"] is False


def test_task_cap_stops_a_long_turn(monkeypatch):
    monkeypatch.setattr(front_mod, "TIER", "deep")
    monkeypatch.setattr(front_mod, "TASK_CAP_S", 0.3)
    h = FakeHermes()
    f, said, events = make(h)
    f.submit("walk to the door")
    assert wait(lambda: h.stopped == ["run1"])  # the watchdog stopped the run in the middle of the turn
    assert wait(lambda: f.run_id is None)
    assert wait(lambda: any("out of time" in s for s in said)) and f.tonyd.stops >= 1
