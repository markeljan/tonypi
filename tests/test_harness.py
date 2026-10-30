"""Harness core with a fake body (real tonyd server over a fake Body) and scripted brains: steering, stop, fallback."""

import asyncio
import base64
import json
import tempfile
import threading
import time
from pathlib import Path

import pytest
from test_tonyd import _Body

from tonypi.harness import backends
from tonypi.motion.library import DATA_DIR as DATA
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
        cut.cancel()
        if task.done() and not task.cancelled():
            return task.result()
        task.cancel()

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
            await asyncio.sleep(0.6)  # a real brain needs a moment to read the words (see STALE_CALL_S)
            await b.tool("run_skill", skill="turn_right", count=1)

    async def main():
        brain = Scripted(script)
        h, d, events = await make(tonyd, brain)
        await h.submit("walk forward 5 steps")
        await asyncio.sleep(0.45)  # second step running
        t = time.monotonic()
        await h.submit("actually, turn right instead")
        await until(lambda: ("turn_right", 1) in d.body.ran)
        assert time.monotonic() - t < 1.2  # the step in progress finishes (+ the brain's 0.6 s), nothing more
        await until(lambda: not h.busy())
        assert "2 of 5 steps done" in brain.results[0] and '"actually, turn right instead"' in brain.results[0]
        walks = [s for s, _ in d.body.ran if s == "walk_forward"]
        assert len(walks) == 2 and d.body.ran[-1] == ("settle", 1)
        assert [e["id"] for e in events if e["type"] == "done"] == [1, 2]
        assert len(brain.texts) == 1  # words came in the tool result: no second turn

    asyncio.run(main())


def test_batched_calls_after_new_words_are_refused(tonyd):
    async def script(b, text):
        if text.startswith("NEW"):
            return
        await b.tool("run_skill", skill="walk_forward", count=3)  # one JS cell / one batch: no thinking between
        await b.tool("run_skill", skill="turn_left")
        await asyncio.sleep(0.6)  # the brain read the words and decided
        await b.tool("run_skill", skill="turn_right")

    async def main():
        brain = Scripted(script)
        h, d, _ = await make(tonyd, brain)
        await h.submit("walk forward 3 steps, then turn left")
        await asyncio.sleep(0.1)
        await h.submit("actually, turn right instead")
        await until(lambda: not h.busy())
        assert brain.results[1].startswith("NOT done") and "turn right instead" in brain.results[1]
        assert ("turn_left", 1) not in d.body.ran and ("turn_right", 1) in d.body.ran

    asyncio.run(main())


def test_parallel_calls_queued_behind_a_cut_walk_are_refused(tonyd):
    async def script(b, text):
        if text.startswith("NEW"):
            return
        await asyncio.gather(b.tool("run_skill", skill="walk_forward", count=3),
                             b.tool("run_skill", skill="turn_left"))

    async def main():
        brain = Scripted(script)
        h, d, _ = await make(tonyd, brain)
        await h.submit("walk forward 3 steps, then turn left")
        await asyncio.sleep(0.45)
        await h.submit("actually, turn right instead")
        await until(lambda: not h.busy())
        assert ("turn_left", 1) not in d.body.ran
        assert d.body.ran.count(("walk_forward", 1)) == 2

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


def test_brain_written_moves(tonyd):
    async def main():
        d, client = tonyd
        h, _, _ = await make(tonyd, Scripted(None))
        motion = json.loads((await h.call_tool("get_motion", {"name": "turn_left"})).text)
        assert motion["source"] == "default" and all(set(f["pos"]) <= {str(i) for i in range(1, 17)}
                                                     for f in motion["frames"])
        raw = json.loads((DATA / "turn_left.json").read_text())["frames"]  # a factory frame, 17/18 included
        assert (await h.call_tool("play_frames", {"frames": raw[:2]})).text.startswith("play_frames: done")
        r = await h.call_tool("save_motion", {"name": "my_step", "frames": motion["frames"]})
        assert not r.error, r.text
        assert not (await h.call_tool("save_skill", {"name": "my_turn", "motions": ["*my_step"]})).error
        assert "my_turn" in json.loads((await h.call_tool("list_skills", {})).text)
        r = await h.call_tool("run_skill", {"skill": "my_turn", "count": 3})
        assert r.text.startswith("my_turn: 3 of 3 steps done")
        assert d.body.ran.count(("my_turn", 1)) == 3  # walked one step per job, like the built-in gaits
        bad = await h.call_tool("save_motion", {"name": "Bad Name", "frames": motion["frames"]})
        assert bad.error and bad.text.startswith("refused")
        assert (await h.call_tool("run_skill", {"skill": "fly"})).error

    asyncio.run(main())


def test_falls_are_the_harness_job(tonyd):
    async def main():
        d, client = tonyd
        guards = []
        orig = d.body.run
        d.body.run = lambda skill, count, say, guard_tilt=None: (guards.append((skill, guard_tilt)),
                                                                 orig(skill, count, say))[1]
        h, _, events = await make(tonyd, Scripted(None))
        r = await h.call_tool("run_skill", {"skill": "wave"})
        await until(lambda: d.body.ran[-1] == ("settle", 1))
        assert not r.error and d.body.ran[-2:] == [("wave", 1), ("settle", 1)]  # back to a stand after a move
        assert guards[-1] == ("wave", 70.0)
        await h.call_tool("run_skill", {"skill": "nod"})
        assert d.body.ran[-1] == ("nod", 1)  # head moves: no settle
        await h.call_tool("run_skill", {"skill": "turn_left", "guard_tilt_deg": 30})
        assert guards[-1] == ("turn_left", 30.0)

        d.body.lying = lambda: True  # Tony on the floor
        n = len(d.body.ran)
        r = await h.call_tool("run_skill", {"skill": "walk_forward", "count": 2})
        assert r.error and "get_up first" in r.text and len(d.body.ran) == n  # refused, nothing moved
        r = await h.call_tool("run_skill", {"skill": "get_up"})
        assert r.error and "Still lying" in r.text  # the fake body stays down
        assert guards[-1] == ("get_up", None)  # no tilt guard getting up from the floor

        d.body.lying = lambda: False
        orig_wave = d.body.run

        def falls(skill, count, say, guard_tilt=None):  # bow knocks him over, get_up works
            ok = orig_wave(skill, count, say, guard_tilt)
            down = skill != "get_up"
            d.body.lying = lambda: down
            return ok
        d.body.run = falls
        r = await h.call_tool("run_skill", {"skill": "bow"})
        assert r.error and "FELL" in r.text and "got you up again" in r.text  # the harness got him up, no model
        assert [s for s, _ in d.body.ran[-2:]] == ["bow", "get_up"]
        assert any(e["type"] == "fall" and e["recovered"] for e in events)

        r = await h.call_tool("trial", {"skill": "bow", "trials": 2})  # measured trials: each from a stand
        res = json.loads(r.text)
        assert len(res["trials"]) == 2 and res["falls"] == 2 and all(t["fell"] for t in res["trials"])

    asyncio.run(main())


def test_no_settle_after_stop(tonyd):
    async def script(b, text):
        await b.tool("run_skill", skill="dance")

    async def main():
        tonyd[0].body.seconds = 5
        brain = Scripted(script)
        h, d, _ = await make(tonyd, brain)
        await h.submit("dance")
        await until(lambda: d.current is not None)
        await h.submit("stop")
        await until(lambda: not h.busy())
        await asyncio.sleep(0.3)
        assert ("settle", 1) not in d.body.ran

    asyncio.run(main())


@pytest.fixture(autouse=True)
def tasklog(monkeypatch, tmp_path):
    from tonypi.harness import tasks
    monkeypatch.setattr(tasks, "LOG", tmp_path / "tasks.jsonl")
    return tmp_path / "tasks.jsonl"


def test_task_loop_persists_until_done(tonyd, tasklog):
    from tonypi.harness.tasks import Outcome

    async def script(b, text):
        await b.tool("run_skill", skill="wave")
        if len(b.texts) < 3:
            return Outcome("continue", "", "still looking", "")
        return Outcome("done", "Found it.", "scanned, turned, walked", "scan before walking")

    async def main():
        brain = Scripted(script)
        h, d, events = await make(tonyd, brain)
        lessons = []
        h.on_lesson = lambda lesson, task: lessons.append((lesson, task))
        await h.submit("find the chair")
        await until(lambda: not h.busy())
        assert len(brain.texts) == 3 and brain.texts[1].startswith("[Harness: the task isn't done yet")
        assert lessons == [("scan before walking", "find the chair")]
        rec = json.loads(tasklog.read_text().splitlines()[-1])
        assert rec["status"] == "done" and rec["continues"] == 2 and rec["tool_calls"] == 3
        assert rec["summary"] == "scanned, turned, walked" and rec["first_action_s"] is not None

    asyncio.run(main())


def test_give_up_is_pushed_back_twice(tonyd, tasklog):
    from tonypi.harness.tasks import Outcome

    async def script(b, text):
        return Outcome("give_up", "I can't.", "looked around once", "")

    async def main():
        brain = Scripted(script)
        h, _, _ = await make(tonyd, brain)
        await h.submit("find the red cup")
        await until(lambda: not h.busy())
        assert len(brain.texts) == 3 and brain.texts[1].startswith("[Harness: don't give up yet")
        assert json.loads(tasklog.read_text().splitlines()[-1])["status"] == "give_up"

    asyncio.run(main())


def test_task_cap_cuts_the_brain(tonyd, tasklog, monkeypatch):
    from tonypi.harness import core

    async def script(b, text):
        await asyncio.sleep(30)

    async def main():
        monkeypatch.setattr(core, "TASK_CAP_S", 0.3)
        brain = Scripted(script)
        h, _, _ = await make(tonyd, brain)
        await h.submit("go to the door")
        await until(lambda: not h.busy())
        assert brain.cuts == 1 and json.loads(tasklog.read_text().splitlines()[-1])["status"] == "timeout"

    asyncio.run(main())


@pytest.fixture
def repo(monkeypatch, tmp_path):
    """learning.py on a throwaway git repo (never commits into the real branch)."""
    import subprocess

    from tonypi.harness import learning
    r = tmp_path / "repo"
    (r / "src/tonypi/harness").mkdir(parents=True)
    (r / "src/tonypi/harness/x.py").write_text("X = 1\n")
    (r / "README").write_text("hi\n")
    for cmd in (["init", "-q"], ["add", "-A"], ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init"]):
        subprocess.run(["git", *cmd], cwd=r, check=True)
    monkeypatch.setenv("GIT_AUTHOR_NAME", "t"), monkeypatch.setenv("GIT_AUTHOR_EMAIL", "t@t")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "t"), monkeypatch.setenv("GIT_COMMITTER_EMAIL", "t@t")
    monkeypatch.setattr(learning, "REPO", r)
    monkeypatch.setattr(learning, "LESSONS", r / "src/tonypi/harness/learned.md")
    monkeypatch.setattr(learning, "EXPORT", r / "data/learned_own")
    monkeypatch.setattr(learning, "USER_DIR", tmp_path / "user")
    return r


def test_lessons_persist_commit_and_reach_the_brain(repo):
    from tonypi.harness import learning
    from tonypi.harness.core import Harness
    h = Harness([], [Scripted(None)])
    lp = learning.Learning()
    lp.attach(h)
    assert lp.add_lesson("Scan before walking toward a target.", "go to the chair")
    assert not lp.add_lesson("scan before walking toward a target.")  # dedupe
    assert "Scan before walking" in h.system_prompt() and h.notes and "Scan before walking" in h.notes[0]
    assert "learned" in learning.git("log", "-1", "--format=%s")


def test_learned_moves_are_exported(repo, tmp_path):
    from tonypi.harness import learning
    (tmp_path / "user/motions").mkdir(parents=True)
    (tmp_path / "user/motions/jack_own.json").write_text('{"frames": []}')
    (tmp_path / "user/motions/other.json").write_text('{"frames": []}')
    (tmp_path / "user/skills.json").write_text('{"jack_own": {"motions": ["jack_own"]}, "x_pi": {"motions": []}}')
    learning.Learning().after_task({"created": ["motion:jack_own"]})
    assert (repo / "data/learned_own/motions/jack_own.json").exists()
    assert not (repo / "data/learned_own/motions/other.json").exists()
    assert list(json.loads((repo / "data/learned_own/skills.json").read_text())) == ["jack_own"]
    assert "learned moves" in learning.git("log", "-1", "--format=%s")


@pytest.mark.parametrize("edit,tests_ok,kept", [
    ("src/tonypi/harness/x.py", True, True),   # good change: committed, reload pending
    ("src/tonypi/harness/x.py", False, False),  # tests fail: reverted
    ("README", True, False),                    # outside the harness: reverted
])
def test_improve_code(repo, monkeypatch, tmp_path, edit, tests_ok, kept):
    import sys
    import threading as th

    from tonypi.harness import learning
    from tonypi.harness.peripherals import Ctx
    fake = tmp_path / "dev.py"
    fake.write_text(f"import pathlib; p = pathlib.Path({str(repo / edit)!r}); p.write_text('X = 2\\n')\n")
    monkeypatch.setenv("TONYPI_DEV_CMD", f"{sys.executable} {fake}")
    monkeypatch.setattr(learning, "TEST_CMD", ["true" if tests_ok else "false"])
    lp = learning.Learning()
    res = lp.improve_code({"request": "make X two, it reads better"}, Ctx(th.Event(), lambda *a, **k: None))
    assert (repo / edit).read_text() == ("X = 2\n" if kept else ("X = 1\n" if edit.endswith(".py") else "hi\n"))
    assert res.error is not kept and lp.reload_pending is kept
    assert ("self-update" in learning.git("log", "-1", "--format=%s")) is kept


def test_backend_order(monkeypatch):
    monkeypatch.setenv("TONYPI_BRAIN", "local,claude")
    assert [b.name for b in backends.build()] == ["local"]
    assert [b.name for b in backends.build("claude")] == ["claude", "local"]
    monkeypatch.delenv("TONYPI_BRAIN")
    assert [b.name for b in backends.build()] == ["codex", "local"]


def test_codex_tiers(monkeypatch):
    from tonypi.harness.backends import codex
    monkeypatch.setattr(codex, "FORCE_TIER", "auto")
    assert codex.tier_for("wave") == "fast"
    assert codex.tier_for("how's your battery and how hot are you?") == "multi"
    assert codex.tier_for("walk forward 3 steps, then turn left") == "multi"
    assert codex.tier_for("find the door and walk toward it") == "advanced"
    assert codex.tier_for("wave", failed_before=True) == "advanced"
    monkeypatch.setattr(codex, "FORCE_TIER", "fast")
    assert codex.tier_for("find the door") == "fast"


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
        assert [e["text"] for e in events if e["type"] == "say"][0].startswith("Hi! wave: done, max tilt")
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
