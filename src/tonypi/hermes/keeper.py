"""Idle posture settling only; falls are handled explicitly by the agent/user.

Automatic fall recovery is disabled, including for intentionally horizontal poses.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Callable

log = logging.getLogger("tonypi.hermes.keeper")
SETTLE_AFTER_S = float(os.environ.get("TONYPI_SETTLE_AFTER_S", "0.6"))
AUTO_GETUP = False
STOP_GRACE_S = 10.0
MAX_GETUPS = 2
POLL_S = 0.25
NO_SETTLE = {"look_left", "look_right", "look_up", "look_down", "look_center", "nod", "shake_head",
             "stand", "settle", "get_up", "lie_down", "sleep"}
LIE_SKILLS = {"lie_down", "sleep"}
UP_SKILLS = {"get_up", "stand"}


class BodyKeeper:
    def __init__(self, tonyd, say: Callable[[str], None], on_fall: Callable[[str], None] | None = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.tonyd = tonyd
        self.say = say
        self.on_fall = on_fall
        self.clock = clock
        self.lock = threading.Lock()
        self.last_job_end = 0.0
        self.running = False
        self.settle_due: float | None = None
        self.meant_to_lie = False
        self.last_stop = -1e9
        self.getups: list[float] = []
        self.helpless = False
        self.events: list[dict] = []

    def on_job(self, ev: dict) -> None:
        skill, state = ev.get("skill", ""), ev.get("state", "")
        now = self.clock()
        with self.lock:
            if state == "running":
                self.running = True
                self.settle_due = None
                if skill in LIE_SKILLS:
                    self.meant_to_lie = True
                elif skill in UP_SKILLS or skill not in NO_SETTLE:
                    self.meant_to_lie = False
                if skill in UP_SKILLS:
                    self.helpless = False
            elif state in ("done", "failed", "cancelled"):
                self.running = False
                self.last_job_end = now
                if state != "cancelled" and skill not in NO_SETTLE and not ev.get("hold_s"):
                    self.settle_due = now + SETTLE_AFTER_S

    def note_stop(self) -> None:
        with self.lock:
            self.last_stop = self.clock()
            self.settle_due = None

    def start(self) -> None:
        threading.Thread(target=self._loop, name="body-keeper", daemon=True).start()

    def _loop(self) -> None:
        while True:
            try:
                self.tick()
            except Exception as e:
                log.debug("keeper: %s", e)
            time.sleep(POLL_S)

    def tick(self) -> str | None:
        """Settle idle upright bodies; never stop or get up in response to tilt."""
        now = self.clock()
        st = self.tonyd.state()
        lying = bool(st.get("lying"))
        with self.lock:
            if lying:
                self.settle_due = None
                return None
            if self.settle_due is None or now < self.settle_due or self.running:
                return None
            self.settle_due = None
        self.tonyd.run("settle", 1, wait=False)
        return "settle"
