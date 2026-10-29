"""Local fallback: today's LFM3-Omni planner (tonypi.agent.brain) on llama-server :8080, offline, ~2-4 s.

The planner can't call tools in a loop; it returns a short plan (say + up to 6 skill actions), executed here through
the harness's tools, so steering and safety work the same. Moves start while the plan is still streaming.
New words mid-task cut the plan at the next safe point and re-plan from the new words (the planner has no memory
of the old task beyond its one-turn follow-up history).
"""

from __future__ import annotations

import asyncio
import json
import queue
import tempfile
import threading
import time

from tonypi.agent import vision
from tonypi.agent.brain import Brain


class LocalBackend:
    name = "local"
    injects = False

    def __init__(self) -> None:
        self.brain = Brain()
        self.cancel = threading.Event()  # this run's; a fresh one per run
        self.h = None

    def configured(self) -> bool:
        return True

    def available(self) -> bool:
        return True  # if llama-server is down, the planner's keyword rules still answer

    async def start(self, harness) -> None:
        self.h = harness

    async def run(self, text: str, source: str) -> None:
        self.cancel = threading.Event()
        await asyncio.to_thread(self._execute, text, self.cancel)

    async def cut(self) -> None:
        self.cancel.set()

    def followup_text(self, msgs: list[str]) -> str:
        return msgs[-1]

    def _execute(self, text: str, cancel: threading.Event) -> None:
        h, t = self.h, time.monotonic()
        todo: queue.Queue = queue.Queue()

        def stopping() -> bool:
            return cancel.is_set() or h.stopped

        def planner() -> None:
            try:
                plan = self.brain.plan_stream(text, on_action=lambda n, c: todo.put((n, c)), cancel=cancel)
                if stopping():
                    return
                h.emit("plan", actions=plan.actions, say=plan.say, source=f"local {plan.source}",
                       seconds=round(time.monotonic() - t, 1))
                if plan.say:
                    h.say(plan.say)
                for a in plan.actions[plan.early:]:
                    todo.put(a)
            finally:
                todo.put(None)

        threading.Thread(target=planner, name="local-planner", daemon=True).start()
        while True:
            try:
                item = todo.get(timeout=0.05)
            except queue.Empty:
                item = ()
            if stopping() or item is None:
                return  # cut: the planner finishes (or aborts) on its own; nothing more of this plan runs
            if not item:
                continue
            name, count = item
            if name == "stop":
                continue
            if name == "describe_view":
                self._describe(cancel, text)
            elif name == "report_status":
                self._status()
            else:
                h.call_tool_sync("run_skill", {"skill": name, "count": count})

    def _describe(self, cancel: threading.Event, request: str) -> None:
        res = self.h.call_tool_sync("snapshot", {})
        if not res.images:
            self.h.say("My camera isn't working right now.")
            return
        with tempfile.NamedTemporaryFile(suffix=".jpg") as f:
            f.write(res.images[0])
            f.flush()
            desc = vision.describe(f.name, cancel, vision.PROMPT + f' You were asked: "{request}". Answer that '
                                   "first (yes or no if it is a yes/no question), then describe briefly.")
        if desc and not (cancel.is_set() or self.h.stopped):
            self.h.say(desc)

    def _status(self) -> None:
        st = json.loads(self.h.call_tool_sync("get_state", {}).text)
        parts = []
        if st.get("tilt_deg") is not None:
            parts.append("I'm lying down." if st["lying"] else f"I'm upright, tilted {st['tilt_deg']:.0f} degrees.")
        if st.get("battery_v"):
            parts.append(f"My battery is at {st['battery_v']:.1f} volts.")
        if st.get("hottest_servo"):
            parts.append(f"My warmest joint is my {st['hottest_servo']['joint']}, at "
                         f"{st['hottest_servo']['temp_c']} degrees.")
        self.h.say(" ".join(parts) or "I can't read my sensors right now.")
