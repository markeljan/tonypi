"""The harness: one brain session at a time, every peripheral as a tool, steerable while it works.

Flow for each utterance (typed, spoken, later from the iPhone app):
  - "stop" (and friends): body, speech and brain halt at once. No LLM is involved, ever.
  - idle: a new task starts on the best available backend (Claude, else the local LFM3 planner).
  - busy: it steers the running task. The words go to a pending slot and `steer` is raised:
      * a tool that is running sees its `ctx.steer`, stops at a safe point (walking: after the current step) and its
        result carries the new words to the brain ("[While this ran, the user said: ...]");
      * a motion tool that is about to start is held back and returns the new words instead;
      * if no tool picks them up within STEER_GRACE_S (the model is mid-thought), the backend is cut
        (interrupt) and the words are sent as the next message of the same session, or (STEER_MODE=steer)
        passed into the running turn (Codex turn/steer).
    Backends that can't take words mid-turn (local LFM3) are cut at once and re-plan from the new words.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import os
import threading
import time
from typing import Callable

from tonypi.agent.brain import STOP_RE
from tonypi.harness.peripherals import Ctx, Peripheral, Result, Tool

log = logging.getLogger("tonypi.harness")
STEER_GRACE_S = 0.3
# words that arrive while the brain thinks (no tool running): "cut" = interrupt + send them as the next message;
# "steer" = hand them to the running turn (backends with steer(), e.g. Codex turn/steer), cut if that fails
STEER_MODE = os.environ.get("TONYPI_STEER_MODE", "cut")

SYSTEM = """You are Tony, a small (35 cm) humanoid robot, a Hiwonder TonyPi with a Raspberry Pi 5 inside. You live \
with your owner at home. People talk to you by voice ("hey Tony, ...") or by text; voice comes through \
speech-to-text, so expect small transcription errors and guess the obvious meaning.

You act through tools. Do the task instead of describing it, and do exactly what was asked (no extra moves). \
Before you claim anything about your surroundings, look with the camera. When a task needs several steps \
(look, move, look again), keep going on your own until it is done or clearly impossible, then say briefly how \
it went. Speak while you work only when it helps (one short sentence).

New messages can arrive while you are working, inside a tool result ("[While this ran, the user said: ...]") or \
as a new message. The newest instruction wins: if it changes the task, drop the old plan at once and do the new \
one; if it is just a question, answer it and carry on.

Safety is handled by the body, not by you: it refuses moves when servos are too hot or when lying down and tells \
you why. Assume you may be on a table: walk only when asked to. Never invent robot capabilities you don't have \
tools for.
"""


class BrainUnavailable(RuntimeError):
    """The backend can't serve this request (cloud down, not logged in): the harness falls back to the next."""


class Harness:
    def __init__(self, peripherals: list[Peripheral], backends: list, emit: Callable[..., None] | None = None,
                 on_task_done: Callable[[str], None] | None = None) -> None:
        self.peripherals = {p.name: p for p in peripherals}
        self.tools: dict[str, Tool] = {}
        for p in peripherals:
            for t in p.tools():
                if t.name in self.tools:
                    raise ValueError(f"tool {t.name!r} defined twice")
                self.tools[t.name] = t
        self.backends = backends  # preference order; the last one is the always-available fallback
        self._emit = emit or (lambda type_, **kw: None)
        self.on_task_done = on_task_done
        self.ids = itertools.count(1)
        self.loop: asyncio.AbstractEventLoop | None = None
        self.task: asyncio.Task | None = None
        self.task_ids: list[int] = []
        self.backend = None
        self.pending: list[str] = []
        self.active: set[threading.Event] = set()  # ctx.steer of each running tool call
        self.world = asyncio.Lock()  # Tool.ordered: one at a time, FIFO (look, then the photo)
        self.tools_running = 0
        self.stopped = False

    # ---- plumbing ---------------------------------------------------------------------------------------------

    def emit(self, type_: str, **kw) -> None:
        if type_ in ("action", "say", "info", "plan") and "id" not in kw and self.task_ids:
            kw["id"] = self.task_ids[0]
        self._emit(type_, **kw)

    def system_prompt(self) -> str:
        parts = [SYSTEM] + [p.prompt for p in self.peripherals.values() if p.prompt]
        return "\n\n".join(parts)

    def say(self, text: str) -> None:
        voice = self.peripherals.get("voice")
        if voice:
            voice.speak(text)
        else:
            self.emit("say", text=text)

    async def start(self) -> None:
        self.loop = asyncio.get_running_loop()
        for p in self.peripherals.values():
            p.start()
        for b in self.backends:
            if b.configured():
                await b.start(self)

    def busy(self) -> bool:
        return self.task is not None and not self.task.done()

    # ---- input ------------------------------------------------------------------------------------------------

    async def submit(self, text: str, source: str = "text") -> int:
        rid = next(self.ids)
        text = text.strip()
        self.emit("heard", id=rid, source=source, text=text)
        if not text:
            self.emit("done", id=rid)
            return rid
        if STOP_RE.match(text):
            await self.stop(rid)
            return rid
        if self.busy():
            self.emit("info", id=rid, text="steering the running task")
            await self._steer(rid, text)
        else:
            self.task_ids = [rid]
            self.task = asyncio.create_task(self._run(text, source))
        return rid

    async def stop(self, rid: int | None = None) -> None:
        """Halts body, speech and brain. Never waits for an LLM."""
        t = time.monotonic()
        self.stopped = True
        self.take_pending()
        self._signal_tools()
        for p in self.peripherals.values():
            p.stop()
        self.emit("action", id=rid, name="stop", count=1, ms=round((time.monotonic() - t) * 1000))
        if self.busy() and self.backend:
            try:
                await asyncio.wait_for(self.backend.cut(), 5)
            except Exception as e:
                log.warning("cut after stop: %s", e)
        if rid is not None:
            self.emit("done", id=rid)

    async def _steer(self, rid: int, text: str) -> None:
        self.task_ids.append(rid)
        self.pending.append(text)
        self._signal_tools()
        voice = self.peripherals.get("voice")
        if voice:
            voice.stop()  # they're talking: don't talk over them
        backend = self.backend
        if backend is None:
            return
        if not backend.injects:
            await backend.cut()
            return
        await asyncio.sleep(STEER_GRACE_S)
        if self.pending and self.tools_running == 0 and self.busy():
            if STEER_MODE == "steer" and hasattr(backend, "steer"):
                msgs = self.take_pending()
                if await backend.steer(msgs):
                    self.emit("info", text="new words passed into the running turn")
                    return
                self.pending = msgs + self.pending
            self.emit("info", text="interrupting the brain to pass on the new words")
            await backend.cut()

    def take_pending(self) -> list[str]:
        msgs, self.pending = self.pending, []
        return msgs

    def _signal_tools(self) -> None:
        """Running tools stop at their next safe point. Stays set for that call, even after the words are taken."""
        for ev in list(self.active):
            ev.set()

    # ---- task -------------------------------------------------------------------------------------------------

    async def _pick(self, failed: set[str] = frozenset()):
        for b in self.backends:
            if b.name in failed:
                continue
            if b.configured() and await asyncio.to_thread(b.available):
                return b
            if b is not self.backends[-1] and b.configured():
                self.emit("info", text=f"{b.name} brain unreachable, falling back")
        return self.backends[-1]

    async def _run(self, text: str, source: str) -> None:
        self.stopped = False
        t0 = time.monotonic()
        self.emit("thinking")
        msg: str | None = text
        failed: set[str] = set()
        try:
            while msg is not None and not self.stopped:
                backend = self.backend = await self._pick(failed)
                self.emit("info", text=f"brain: {backend.name}")
                try:
                    await backend.run(msg, source)
                except BrainUnavailable as e:
                    if backend is self.backends[-1]:
                        raise
                    self.emit("info", text=f"{backend.name} failed ({e}); falling back")
                    failed.add(backend.name)
                    continue  # same words, next backend
                msgs = self.take_pending()
                msg = backend.followup_text(msgs) if msgs and not self.stopped else None
        except Exception as e:
            log.exception("task failed")
            self.emit("info", text=f"error: {e}")
        finally:
            body = self.peripherals.get("body")
            if body is not None and not self.stopped:
                await asyncio.to_thread(body.settle)
            self.emit("info", text=f"task finished in {time.monotonic() - t0:.1f} s")
            ids, self.task_ids = self.task_ids, []
            for i in ids:
                self.emit("done", id=i)
            if self.on_task_done:
                self.on_task_done(source)

    # ---- tools (called by backends) ---------------------------------------------------------------------------

    async def call_tool(self, name: str, args: dict, inject: bool = False) -> Result:
        """Runs a peripheral tool. inject: hand pending words to the brain in the result (see module doc)."""
        tool = self.tools.get(name)
        if tool is None:
            return Result(f"unknown tool {name}", error=True)
        if name == "run_skill":
            self.emit("action", name=args.get("skill"), count=int(args.get("count") or 1))
        elif name != "say":
            self.emit("action", name=name, count=1, args=args)
        if self.stopped and tool.moves:
            return Result("not done: the user said stop", error=True)
        if inject and tool.moves and self.pending:
            msgs = self.take_pending()
            return Result(f"NOT done: before it started, the user said: {_quote(msgs)}. "
                          "Follow the newest instruction.", error=True)
        self.tools_running += 1
        steer = threading.Event()
        if self.pending:
            steer.set()
        self.active.add(steer)
        t = time.monotonic()
        try:
            if tool.ordered:
                async with self.world:
                    res = await asyncio.to_thread(tool.handler, args, Ctx(steer, self.emit))
            else:
                res = await asyncio.to_thread(tool.handler, args, Ctx(steer, self.emit))
        except Exception as e:
            log.exception("tool %s failed", name)
            res = Result(f"error: {e}", error=True)
        finally:
            self.tools_running -= 1
            self.active.discard(steer)
        self.emit("tool_result", name=name, text=res.text[:300], images=len(res.images),
                  seconds=round(time.monotonic() - t, 2))
        if inject and self.pending:
            res.text += (f"\n\n[While this ran, the user said: {_quote(self.take_pending())}. The newest instruction "
                         "wins: if it changes the task, drop the old plan.]")
        return res

    def call_tool_sync(self, name: str, args: dict) -> Result:
        """For backends that run in a worker thread."""
        return asyncio.run_coroutine_threadsafe(self.call_tool(name, args), self.loop).result()

    def status(self) -> dict:
        body = self.peripherals.get("body")
        st = body.tonyd.state(fresh=True) if body else {}
        return {**st, "busy": self.busy(), "brain": self.backend.name if self.backend else None}


def _quote(msgs: list[str]) -> str:
    return " / ".join(json.dumps(m) for m in msgs)
