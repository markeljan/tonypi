"""The agent's handle on the body. tonyd does the work; this keeps the small interface the agent needs."""

from __future__ import annotations

import logging
import threading
from typing import Callable

from tonypi.tonyd.client import TonydClient, TonydError

log = logging.getLogger(__name__)

# fine while lying: head moves, and the skills that get up or lie down themselves
IN_PLACE = {"look_left", "look_right", "look_up", "look_down", "look_center", "nod", "shake_head",
            "get_up", "stand", "lie_down", "sleep", "settle"}


class BodyLink:
    def __init__(self, on_warn: Callable[[str], None]) -> None:
        self.cancelled = threading.Event()
        self.on_warn = on_warn
        self.tonyd = TonydClient(on_event=self._on_event)

    def _on_event(self, ev: dict) -> None:
        if ev.get("event") == "warn":
            self.on_warn(ev["text"])

    def cancel(self) -> None:
        self.cancelled.set()
        try:
            self.tonyd.stop()
        except TonydError as e:
            log.warning("stop: %s", e)

    def run(self, skill: str, count: int, say: Callable[[str], None]) -> bool:
        """Runs one skill to the end. Anything the body reports (too hot, ...) is passed to `say`.
        tonyd leaves falls to its brains, so this one gets up first when Tony is lying and has to move his body."""
        if self.cancelled.is_set():
            return False
        try:
            if skill not in IN_PLACE and self.tonyd.state()["lying"]:
                job = self.tonyd.run("get_up", 1)
                for line in job["said"]:
                    say(line)
                if job["state"] != "done" or self.cancelled.is_set():
                    return False
            job = self.tonyd.run(skill, count)
        except TonydError as e:
            log.error("run %s: %s", skill, e)
            say("I'm plugged into my charger, so I can't walk any further." if "tethered" in str(e)
                else "I can't reach my body right now.")
            return False
        for line in job["said"]:
            say(line)
        if job.get("error"):
            log.error("skill %s failed: %s", skill, job["error"])
        return job["state"] == "done"

    def settle(self) -> None:
        if not self.cancelled.is_set():
            try:
                self.tonyd.run("settle")
            except TonydError as e:
                log.warning("settle: %s", e)

    def status(self) -> dict:
        return self.tonyd.state(fresh=True)

    def snapshot(self) -> str:
        return self.tonyd.snapshot()["path"]

    def close(self) -> None:
        self.tonyd.close()
