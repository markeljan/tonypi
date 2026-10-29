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
        self.cancel = threading.Event()
        self.h = None

    def configured(self) -> bool:
        return True

    def available(self) -> bool:
        return True  # if llama-server is down, the planner's keyword rules still answer

    async def start(self, harness) -> None:
        self.h = harness

    async def run(self, text: str, source: str) -> None:
        self.cancel.clear()
        await asyncio.to_thread(self._execute, text)

    async def cut(self) -> None:
        self.cancel.set()

    def followup_text(self, msgs: list[str]) -> str:
        return msgs[-1]

    def _stopping(self) -> bool:
        return self.cancel.is_set() or self.h.stopped

    def _execute(self, text: str) -> None:
        h, t = self.h, time.monotonic()
        todo: queue.Queue = queue.Queue()

        def planner() -> None:
            try:
                plan = self.brain.plan_stream(text, on_action=lambda n, c: todo.put((n, c)))
                h.emit("plan", actions=plan.actions, say=plan.say, source=f"local {plan.source}",
                       seconds=round(time.monotonic() - t, 1))
                if plan.say and not self._stopping():
                    h.say(plan.say)
                for a in plan.actions[plan.early:]:
                    todo.put(a)
            finally:
                todo.put(None)

        threading.Thread(target=planner, name="local-planner", daemon=True).start()
        while (item := todo.get()) is not None:
            if self._stopping():
                continue  # drain
            name, count = item
            if name == "stop":
                continue
            if name == "describe_view":
                self._describe()
            elif name == "report_status":
                self._status()
            else:
                h.call_tool_sync("run_skill", {"skill": name, "count": count})

    def _describe(self) -> None:
        res = self.h.call_tool_sync("snapshot", {})
        if not res.images:
            self.h.say("My camera isn't working right now.")
            return
        with tempfile.NamedTemporaryFile(suffix=".jpg") as f:
            f.write(res.images[0])
            f.flush()
            desc = vision.describe(f.name, self.cancel)
        if desc and not self._stopping():
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
