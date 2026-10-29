"""The agent's handle on the body. tonyd does the work; this keeps the small interface the agent needs."""

from __future__ import annotations

import logging
import threading
from typing import Callable

from tonypi.tonyd.client import TonydClient, TonydError

log = logging.getLogger(__name__)


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
        """Runs one skill to the end. Anything the body reports (too hot, fell over) is passed to `say`."""
        if self.cancelled.is_set():
            return False
        try:
            job = self.tonyd.run(skill, count)
        except TonydError as e:
            log.error("run %s: %s", skill, e)
            say("I can't reach my body right now.")
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
