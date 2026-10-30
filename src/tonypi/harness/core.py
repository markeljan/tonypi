"""The harness: one brain session at a time, every peripheral as a tool, steerable while it works.

Flow for each utterance (typed, spoken, later from the iPhone app):
  - "stop" (and friends): body, speech and brain halt at once. No LLM is involved, ever.
  - idle: a new task starts on the best available backend (Claude, else the local LFM3 planner).
  - busy: it steers the running task. The words go to a pending slot and `steer` is raised:
      * a tool that is running sees its `ctx.steer`, stops at a safe point (walking: after the current step) and its
        result carries the new words to the brain ("[While this ran, the user said: ...]");
      * a motion tool that is about to start is held back and returns the new words instead, and so is one that
        follows a tool result carrying new words faster than the brain could have read them (STALE_CALL_S);
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
from tonypi.harness.tasks import (CONTINUE_PROMPT, LOOP_PROMPT, MAX_CONTINUES, MAX_PUSHBACKS, PLAYBOOK,
                                  PUSHBACK_PROMPT, STUCK_AFTER, STUCK_PROMPT, TASK_CAP_S, TaskLog)

log = logging.getLogger("tonypi.harness")
STEER_GRACE_S = 0.3
# words that arrive while the brain thinks (no tool running): "cut" = interrupt + send them as the next message;
# "steer" = hand them to the running turn (backends with steer(), e.g. Codex turn/steer), cut if that fails
STEER_MODE = os.environ.get("TONYPI_STEER_MODE", "cut")
# a motion call this soon after new words went out in a tool result was issued before the brain read them (several
# calls in one go: Codex runs them from one JS cell, Claude can batch them): refused, the words repeated
STALE_CALL_S = 0.5

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
        self.told: tuple[float, list[str]] = (0.0, [])  # when new words last went out in a tool result, and which
        self.tools_running = 0
        self.stopped = False
        self.timed_out = False
        self.failures: dict[str, int] = {}
        self.tasklog: TaskLog | None = None
        self.on_lesson: Callable[[str, str], None] | None = None  # (lesson, task text): self-update (learning.py)
        self.extra_prompt: Callable[[], str] | None = None  # learned instructions, appended to the system prompt
        self.on_task_end: Callable[[dict], None] | None = None  # (task log record): export learned moves
        self.notes: list[str] = []  # for the running brain session, sent with its next turn (new lessons ...)

    # ---- plumbing ---------------------------------------------------------------------------------------------

    def emit(self, type_: str, **kw) -> None:
        if type_ in ("action", "say", "info", "plan") and "id" not in kw and self.task_ids:
            kw["id"] = self.task_ids[0]
        if self.tasklog is not None:
            self.tasklog.on_event(type_, kw)
        self._emit(type_, **kw)

    def system_prompt(self) -> str:
        parts = [SYSTEM, LOOP_PROMPT] + [p.prompt for p in self.peripherals.values() if p.prompt] + [PLAYBOOK]
        if self.extra_prompt and (extra := self.extra_prompt()):
            parts.append(extra)
        return "\n\n".join(parts)

    def say(self, text: str, final: bool = False) -> None:
        """final: the task's last word (closes the voice request it answers)."""
        voice = self.peripherals.get("voice")
        if voice:
            voice.speak(text, final)
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
        """One task: turns until the brain reports it done (verified), the user says stop, or the cap (tasks.py)."""
        self.stopped = False
        t0 = time.monotonic()
        tl = self.tasklog = TaskLog(text, source)
        self.failures: dict[str, int] = {}  # failed tries per "same thing" in this task (stuck detection)
        self.emit("thinking")
        msg: str | None = text
        failed: set[str] = set()
        continues = pushbacks = 0
        timer = asyncio.create_task(self._deadline(t0))
        for p in self.peripherals.values():  # e.g. head centred: every task starts from the same view
            if hasattr(p, "task_start"):
                try:
                    await asyncio.to_thread(p.task_start)
                except Exception as e:
                    log.warning("%s task_start: %s", p.name, e)
        try:
            while msg is not None and not self.stopped:
                backend = self.backend = await self._pick(failed)
                self.emit("info", text=f"brain: {backend.name}")
                try:
                    outcome = await backend.run(msg, source)
                except BrainUnavailable as e:
                    if backend is self.backends[-1]:
                        raise
                    self.emit("info", text=f"{backend.name} failed ({e}); falling back")
                    failed.add(backend.name)
                    continue  # same words, next backend
                msgs = self.take_pending()
                if msgs and not self.stopped:
                    tl.steers += 1
                    msg = backend.followup_text(msgs)
                    continue
                msg = None
                if outcome is None:  # a backend without the status contract (LFM3): one turn is the task
                    tl.status = "done" if not self.stopped else tl.status
                    break
                tl.summary = outcome.summary or tl.summary
                if outcome.lesson.strip():
                    tl.lessons.append(outcome.lesson.strip())
                    if self.on_lesson:
                        self.on_lesson(outcome.lesson.strip(), text)
                elapsed = time.monotonic() - t0
                if self.timed_out or self.stopped:
                    break
                if outcome.status == "continue" and continues < MAX_CONTINUES:
                    continues += 1
                    tl.continues = continues
                    msg = CONTINUE_PROMPT.format(elapsed=elapsed, cap=TASK_CAP_S)
                    if continues % 3 == 0:  # turn after turn without getting there: the same nudge as for failures
                        msg += "\n" + STUCK_PROMPT.format(n=continues, what="turns that didn't finish the task")
                elif outcome.status == "give_up" and pushbacks < MAX_PUSHBACKS and elapsed < TASK_CAP_S - 30:
                    pushbacks += 1
                    tl.pushbacks = pushbacks
                    msg = PUSHBACK_PROMPT.format(left=TASK_CAP_S - elapsed, summary=outcome.summary)
                else:
                    tl.status = outcome.status if outcome.status != "continue" else "unfinished"
        except Exception as e:
            log.exception("task failed")
            self.emit("info", text=f"error: {e}")
            tl.status = "error"
        finally:
            timer.cancel()
            if self.stopped:
                tl.status = "stopped"
            elif self.timed_out:
                tl.status = "timeout"
            body = self.peripherals.get("body")
            if body is not None and not self.stopped:
                await asyncio.to_thread(body.settle)
            if (voice := self.peripherals.get("voice")) is not None and hasattr(voice, "task_ended"):
                voice.task_ended()
            tl.t_done = time.time()
            self.tasklog = None
            rec = tl.write()
            if self.on_task_end:
                try:
                    await asyncio.to_thread(self.on_task_end, rec)
                except Exception:
                    log.exception("task-end hook failed")
            self.emit("task_log", **{k: rec[k] for k in ("status", "total_s", "first_action_s", "tool_calls",
                                                          "falls", "continues", "pushbacks", "created", "summary")})
            self.emit("info", text=f"task finished in {time.monotonic() - t0:.1f} s ({tl.status})")
            ids, self.task_ids = self.task_ids, []
            for i in ids:
                self.emit("done", id=i)
            if self.on_task_done:
                self.on_task_done(source)

    async def _deadline(self, t0: float) -> None:
        """The task cap: cut the brain mid-turn when time is up (the owner may extend it: TONYPI_TASK_CAP_S)."""
        self.timed_out = False
        await asyncio.sleep(TASK_CAP_S)
        self.timed_out = True
        self.emit("info", text=f"task cap of {TASK_CAP_S:.0f} s reached")
        self.say("I've run out of time for this one.")
        for p in self.peripherals.values():
            p.stop()
        if self.backend:
            await self.backend.cut()

    # ---- tools (called by backends) ---------------------------------------------------------------------------

    async def call_tool(self, name: str, args: dict, inject: bool = False) -> Result:
        """Runs a peripheral tool. inject: hand pending words to the brain in the result (see module doc)."""
        tool = self.tools.get(name)
        if tool is None:
            return Result(f"unknown tool {name}", error=True)
        if tool.ordered:  # checked once it is this call's turn: words may have come while it queued
            async with self.world:
                return await self._call(tool, args, inject)
        return await self._call(tool, args, inject)

    def _refuse(self, tool: Tool, inject: bool) -> Result | None:
        if not tool.moves:
            return None
        if self.stopped:
            return Result("not done: the user said stop", error=True)
        if inject and self.pending:
            msgs = self.take_pending()
            self.told = (time.monotonic(), msgs)
            return Result(f"NOT done: before it started, the user said: {_quote(msgs)}. "
                          "Follow the newest instruction.", error=True)
        if inject and time.monotonic() - self.told[0] < STALE_CALL_S:
            self.told = (time.monotonic(), self.told[1])
            return Result(f"NOT done: this was planned before you read the user's new words: "
                          f"{_quote(self.told[1])}. Re-plan from them.", error=True)
        return None

    async def _call(self, tool: Tool, args: dict, inject: bool) -> Result:
        name = tool.name
        if refused := self._refuse(tool, inject):
            self.emit("refused", name=name, args=args, text=refused.text)
            return refused
        if name == "run_skill":
            self.emit("action", name=args.get("skill"), count=int(args.get("count") or 1))
        elif name != "say":
            self.emit("action", name=name, count=1, args=args)
        self.tools_running += 1
        steer = threading.Event()
        if self.pending:
            steer.set()
        self.active.add(steer)
        t = time.monotonic()
        try:
            res = await asyncio.to_thread(tool.handler, args, Ctx(steer, self.emit))
        except Exception as e:
            log.exception("tool %s failed", name)
            res = Result(f"error: {e}", error=True)
        finally:
            self.tools_running -= 1
            self.active.discard(steer)
        self.emit("tool_result", name=name, text=res.text[:300], images=len(res.images),
                  seconds=round(time.monotonic() - t, 2))
        if res.error and not res.text.startswith(("NOT done", "unknown tool")) and self.tasklog is not None:
            what = f"{name} {args.get('skill', '')}".strip() if name in ("run_skill", "trial") else name
            n = self.failures[what] = self.failures.get(what, 0) + 1
            if n >= STUCK_AFTER:  # 2 failed tries of the same thing: stop repeating it (owner's lesson, rug edge)
                res.text += "\n\n" + STUCK_PROMPT.format(n=n, what=what)
                self.emit("stuck", what=what, tries=n)
        if inject and self.pending:
            msgs = self.take_pending()
            self.told = (time.monotonic(), msgs)
            res.text += (f"\n\n[While this ran, the user said: {_quote(msgs)}. The newest instruction "
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
