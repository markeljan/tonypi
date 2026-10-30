"""Hermes front end routing with fakes: stop never reaches Hermes, commands during a run steer it, late steers replay."""

import queue
import threading
import time

from tonypi.hermes import front as front_mod
from tonypi.hermes.front import Front, tier_for


class FakeTonyd:
    def __init__(self) -> None:
        self.stops = 0

    def stop(self) -> None:
        self.stops += 1

    def state(self, fresh: bool = False) -> dict:
        return {"battery_mv": 12000}


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
        kind = self.release.get(timeout=5)
        pending = "\n".join(self.steered) if self.late else ""
        yield {"event": kind, "output": f"done {run_id}", "pending_steer": pending}

    def steer(self, run_id, text):
        self.steered.append(text)
        return True

    def stop(self, run_id):
        self.stopped.append(run_id)
        self.release.put("run.cancelled")


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
    h.release.put("run.completed")
    assert wait(lambda: f.run_id is None)
    assert wait(lambda: sum(e["type"] == "done" for e in events) == 2)  # both submissions finish with the run


def test_late_steer_becomes_the_next_turn():
    h = FakeHermes(late=True)
    f, said, events = make(h)
    f.submit("wave")
    assert wait(lambda: f.run_id not in (None, "starting"))
    f.submit("and bow")
    h.release.put("run.completed")
    assert wait(lambda: len(h.started) == 2)
    assert h.started[1][0] == "and bow"
    h.late = False
    h.release.put("run.completed")
    assert wait(lambda: f.run_id is None)


def test_tiers(monkeypatch):
    monkeypatch.setattr(front_mod, "TIER", "auto")
    assert tier_for("wave") == "fast"
    assert tier_for("walk forward 3 steps, then turn left") == "fast"
    assert tier_for("find the door and walk toward it") == "deep"
    assert tier_for("go to the kitchen") == "deep"
    monkeypatch.setattr(front_mod, "TIER", "deep")
    assert tier_for("wave") == "deep"


def test_fast_tier_sends_model_and_effort():
    h = FakeHermes()
    f, said, events = make(h)
    f.submit("wave")
    assert wait(lambda: h.started)
    model, effort = front_mod.TIERS[tier_for("wave")]
    assert h.started[0] == ("wave", model, effort)
    h.release.put("run.completed")
    assert wait(lambda: said == ["done run1"])
