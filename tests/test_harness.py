"""Harness core with a fake body (real tonyd server over a fake Body) and scripted brains: steering, stop, fallback."""

import asyncio
import base64
import tempfile
import threading
import time
from pathlib import Path

import pytest
from test_tonyd import _Body

from tonypi.harness import backends
from tonypi.harness.backends.claude import ClaudeBackend
from tonypi.harness.core import BrainUnavailable, Harness
from tonypi.harness.peripherals import Result
from tonypi.harness.peripherals.body import BodyPeripheral
from tonypi.harness.peripherals.voice import VoicePeripheral
from tonypi.tonyd.client import TonydClient
from tonypi.tonyd.server import Tonyd


@pytest.fixture
def tonyd():
    sock = Path(tempfile.mkdtemp()) / "tonyd.sock"
    d = Tonyd(_Body(seconds=0.3)).start()
    threading.Thread(target=d.serve, args=(str(sock),), daemon=True).start()
    for _ in range(100):
        if sock.exists():
            break
        time.sleep(0.01)
    return d, TonydClient(str(sock))


class Scripted:
    """A brain that runs `script(self)` as its turn; records what it sent and got back."""
    name = "scripted"

    def __init__(self, script, injects=True, fail=False):
        self.script, self.injects, self.fail = script, injects, fail
        self.texts, self.results, self.cuts = [], [], 0
        self.cut_ev = asyncio.Event()

    def configured(self):
        return True

    def available(self):
        return True

    async def start(self, h):
        self.h = h

    async def run(self, text, source):
        if self.fail:
            raise BrainUnavailable("offline")
        self.texts.append(text)
        self.cut_ev.clear()
        task = asyncio.create_task(self.script(self, text))
        cut = asyncio.create_task(self.cut_ev.wait())
        await asyncio.wait([task, cut], return_when=asyncio.FIRST_COMPLETED)
        task.cancel()
        cut.cancel()

    async def tool(self, name, **args):
        r = await self.h.call_tool(name, args, inject=self.injects)
        self.results.append(r.text)
        return r

    async def cut(self):
        self.cuts += 1
        self.cut_ev.set()

    def followup_text(self, msgs):
        return "NEW: " + " / ".join(msgs)


async def make(tonyd, *brains):
    d, client = tonyd
    events = []
    h = Harness([BodyPeripheral(client), VoicePeripheral(None)], list(brains),
                emit=lambda type_, **kw: events.append({"type": type_, **kw}))
    await h.start()
    return h, d, events


async def until(cond, timeout=5.0):
    end = time.monotonic() + timeout
    while not cond():
        assert time.monotonic() < end, "timed out"
        await asyncio.sleep(0.02)


def test_steering_lands_between_walk_steps(tonyd):
    async def script(b, text):
        if text.startswith("NEW"):
            return
        await b.tool("run_skill", skill="walk_forward", count=5)
        if "turn right" in b.results[-1]:
            await b.tool("run_skill", skill="turn_right", count=1)

    async def main():
        brain = Scripted(script)
        h, d, events = await make(tonyd, brain)
        await h.submit("walk forward 5 steps")
        await asyncio.sleep(0.45)  # second step running
        t = time.monotonic()
        await h.submit("actually, turn right instead")
        await until(lambda: ("turn_right", 1) in d.body.ran)
        assert time.monotonic() - t < 0.6  # the step in progress finishes, nothing more
        await until(lambda: not h.busy())
        assert "2 of 5 steps done" in brain.results[0] and '"actually, turn right instead"' in brain.results[0]
        walks = [s for s, _ in d.body.ran if s == "walk_forward"]
        assert len(walks) == 2 and d.body.ran[-1] == ("settle", 1)
        assert [e["id"] for e in events if e["type"] == "done"] == [1, 2]
        assert len(brain.texts) == 1  # words came in the tool result: no second turn

    asyncio.run(main())


def test_motion_held_back_when_words_are_pending(tonyd):
    async def script(b, text):
        await asyncio.sleep(0.2)  # "thinking"
        await b.tool("run_skill", skill="dance")

    async def main():
        brain = Scripted(script)
        h, d, _ = await make(tonyd, brain)
        await h.submit("dance")
        await asyncio.sleep(0.05)
        await h.submit("no, bow")
        await until(lambda: not h.busy())
        assert brain.results[0].startswith("NOT done") and "no, bow" in brain.results[0]
        assert not any(s == "dance" for s, _ in d.body.ran)

    asyncio.run(main())


def test_thinking_brain_is_cut_and_gets_the_words_next(tonyd):
    async def script(b, text):
        if text.startswith("NEW"):
            await b.tool("run_skill", skill="turn_left")
            return
        await asyncio.sleep(10)  # long thought, no tool call

    async def main():
        brain = Scripted(script)
        h, d, _ = await make(tonyd, brain)
        await h.submit("go to the kitchen")
        await asyncio.sleep(0.1)
        t = time.monotonic()
        await h.submit("turn left")
        await until(lambda: ("turn_left", 1) in d.body.ran)
        assert time.monotonic() - t < 1.5 and brain.cuts == 1
        assert brain.texts == ["go to the kitchen", "NEW: turn left"]

    asyncio.run(main())


def test_stop_needs_no_brain(tonyd):
    async def script(b, text):
        await b.tool("run_skill", skill="dance")
        await b.tool("run_skill", skill="bow")  # must not run after stop

    async def main():
        tonyd[0].body.seconds = 5
        brain = Scripted(script)
        h, d, events = await make(tonyd, brain)
        await h.submit("dance")
        await until(lambda: d.current is not None)
        t = time.monotonic()
        await h.submit("stop")
        await until(lambda: d.current is None and d.queue.empty())
        assert time.monotonic() - t < 0.5
        await until(lambda: not h.busy())
        assert [s for s, _ in d.body.ran] == ["dance"]  # no bow, no settle
        assert any(e["type"] == "action" and e["name"] == "stop" for e in events)

    asyncio.run(main())


def test_falls_back_when_the_cloud_is_down(tonyd):
    async def script(b, text):
        await b.tool("run_skill", skill="wave")

    async def main():
        cloud, local = Scripted(script, fail=True), Scripted(script, injects=False)
        h, d, events = await make(tonyd, cloud, local)
        await h.submit("wave")
        await until(lambda: not h.busy())
        assert local.texts == ["wave"] and ("wave", 1) in d.body.ran
        assert any("falling back" in e.get("text", "") for e in events)

    asyncio.run(main())


def test_non_injecting_brain_is_cut_and_replans(tonyd):
    async def script(b, text):
        await b.tool("run_skill", skill="walk_forward" if text == "walk" else "turn_right", count=3)

    async def main():
        brain = Scripted(script, injects=False)
        h, d, _ = await make(tonyd, brain)
        await h.submit("walk")
        await asyncio.sleep(0.4)
        await h.submit("turn right")
        await until(lambda: not h.busy())
        assert brain.texts == ["walk", "NEW: turn right"] and brain.cuts == 1
        assert d.body.ran.count(("walk_forward", 1)) == 2 and d.body.ran.count(("turn_right", 1)) == 3

    asyncio.run(main())


def test_backend_order(monkeypatch):
    monkeypatch.setenv("TONYPI_BRAIN", "local,claude")
    assert [b.name for b in backends.build()] == ["local"]
    assert [b.name for b in backends.build("claude")] == ["claude", "local"]
    monkeypatch.delenv("TONYPI_BRAIN")
    assert [b.name for b in backends.build()] == ["codex", "local"]


def test_claude_tool_results_carry_images():
    class H:
        async def call_tool(self, name, args, inject):
            assert inject
            return Result("photo", [b"\xff\xd8jpeg"])

    b = ClaudeBackend()
    b.h = H()
    out = asyncio.run(b._handler("snapshot")({}))
    assert out["content"][0] == {"type": "text", "text": "photo"}
    assert out["content"][1]["mimeType"] == "image/jpeg"
    assert base64.b64decode(out["content"][1]["data"]) == b"\xff\xd8jpeg"


def test_claude_unconfigured_without_token(monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    assert not ClaudeBackend().configured()


@pytest.fixture
def fake_codex(monkeypatch, tmp_path):
    import sys

    from tonypi.harness.backends import codex
    (tmp_path / "auth.json").write_text("{}")
    monkeypatch.setattr(codex, "CODEX_HOME", tmp_path)
    monkeypatch.setattr(codex, "PROBE", "127.0.0.1:1")  # unused: available() is patched below
    monkeypatch.setenv("TONYPI_CODEX_CMD", f"{sys.executable} {Path(__file__).parent / 'fakes/codex_app_server.py'}")
    monkeypatch.setattr(codex.CodexBackend, "available", lambda self: True)
    return codex


class _Camera:
    name, prompt = "camera", ""

    def tools(self):
        from tonypi.harness.peripherals import Tool
        return [Tool("snapshot", "photo", lambda a, c: Result("photo", [b"\xff\xd8x"]))]

    def start(self):
        pass

    def stop(self):
        pass


def test_codex_tool_loop_and_speech(tonyd, fake_codex):
    async def main():
        d, client = tonyd
        events = []
        cx = fake_codex.CodexBackend()
        h = Harness([BodyPeripheral(client), _Camera()], [cx, Scripted(None)],
                    emit=lambda type_, **kw: events.append({"type": type_, **kw}))
        await h.start()
        await h.submit("wave")
        await until(lambda: any(e["type"] == "done" for e in events), 10)
        assert ("wave", 1) in d.body.ran
        assert [e["text"] for e in events if e["type"] == "say"] == ["Hi! wave: done"]
        assert any(e["type"] == "usage" and e["input"] == 50 and e["cache_read"] == 50 for e in events)
        await h.submit("look")
        await until(lambda: sum(e["type"] == "done" for e in events) == 2, 10)
        assert [e["text"] for e in events if e["type"] == "say"][-1] == "1 image"
        if cx.proc:
            cx.proc.kill()

    asyncio.run(main())


def test_codex_401_falls_back(tonyd, fake_codex):
    async def script(b, text):
        await b.tool("run_skill", skill="bow")

    async def main():
        d, client = tonyd
        cx, local = fake_codex.CodexBackend(), Scripted(script, injects=False)
        h = Harness([BodyPeripheral(client)], [cx, local], emit=lambda type_, **kw: None)
        await h.start()
        await h.submit("deny me")
        await until(lambda: local.texts == ["deny me"] and not h.busy(), 10)
        assert ("bow", 1) in d.body.ran and time.monotonic() < cx.down_until
        if cx.proc:
            cx.proc.kill()

    asyncio.run(main())


def test_codex_thinking_turn_is_interrupted_by_new_words(tonyd, fake_codex):
    async def main():
        d, client = tonyd
        cx = fake_codex.CodexBackend()
        h = Harness([BodyPeripheral(client)], [cx, Scripted(None)], emit=lambda type_, **kw: None)
        await h.start()
        await h.submit("think hard")
        await until(lambda: cx.turn_id is not None and not cx.turn_done.is_set(), 10)
        await h.submit("wave instead")
        await until(lambda: ("wave", 1) in d.body.ran, 10)
        if cx.proc:
            cx.proc.kill()

    asyncio.run(main())
