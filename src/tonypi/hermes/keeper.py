"""BodyKeeper: the falls and posture rules tonyd left to the harness (main ef99364), done without any LLM.

- Settle: SETTLE_AFTER_S after a body move ends (done or failed, not cancelled: that was a stop or a newer command)
  with no new job started, run "settle" (neutral stand), so Tony goes limp (tonyd: 1.5 s after the last job) from a
  stable stance instead of mid-stride.
- Fall: when tonyd says `lying` but nothing asked Tony to lie down or sleep, stop the body, say so, tell the running
  Hermes turn, and run get_up. At most MAX_GETUPS tries per minute, then ask for help; never within STOP_GRACE_S of a
  user's "stop" (someone may be holding him).
Hermes can still do all of this itself (get_state, get_up, settle, guard_tilt_deg); this is the floor under it.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Callable

log = logging.getLogger("tonypi.hermes.keeper")

SETTLE_AFTER_S = float(os.environ.get("TONYPI_SETTLE_AFTER_S", "1.0"))
AUTO_GETUP = os.environ.get("TONYPI_HERMES_AUTO_GETUP", "1") != "0"
STOP_GRACE_S = 10.0
MAX_GETUPS = 2
POLL_S = 0.5
# no settle after these: head-only, already a stand, or meant to end lying down
NO_SETTLE = {"look_left", "look_right", "look_up", "look_down", "look_center", "nod", "shake_head",
             "stand", "settle", "get_up", "lie_down", "sleep"}
LIE_SKILLS = {"lie_down", "sleep"}
UP_SKILLS = {"get_up", "stand"}


class BodyKeeper:
    def __init__(self, tonyd, say: Callable[[str], None], on_fall: Callable[[str], None] | None = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.tonyd = tonyd
        self.say = say
        self.on_fall = on_fall  # gets a note for the running Hermes turn
        self.clock = clock
        self.lock = threading.Lock()
        self.last_job_end = 0.0
        self.running = False  # a tonyd job is running
        self.settle_due: float | None = None
        self.meant_to_lie = False  # last posture skill was lie_down / sleep
        self.last_stop = -1e9
        self.getups: list[float] = []
        self.helpless = False  # gave up getting up; waits for a human or an up-skill

    # ---- inputs -----------------------------------------------------------------------------------------------

    def on_job(self, ev: dict) -> None:
        """tonyd job event (subscribe stream)."""
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
        """The user said stop: no settle (stay where stopped), no automatic get-up for a while."""
        with self.lock:
            self.last_stop = self.clock()
            self.settle_due = None

    # ---- the loop ---------------------------------------------------------------------------------------------

    def start(self) -> None:
        threading.Thread(target=self._loop, name="body-keeper", daemon=True).start()

    def _loop(self) -> None:
        while True:
            try:
                self.tick()
            except Exception as e:  # tonyd restarting etc.
                log.debug("keeper: %s", e)
            time.sleep(POLL_S)

    def tick(self) -> str | None:
        """One check; returns what it did (for tests): "settle", "get_up", "help" or None."""
        now = self.clock()
        st = self.tonyd.state()
        lying = bool(st.get("lying")) and not st.get("asleep")
        with self.lock:
            fallen = lying and not self.meant_to_lie and not self.running
            if fallen:
                self.settle_due = None
                if self.helpless or not AUTO_GETUP or now - self.last_stop < STOP_GRACE_S:
                    return None
                self.getups = [t for t in self.getups if now - t < 60]
                if len(self.getups) >= MAX_GETUPS:
                    self.helpless = True
                    act = "help"
                else:
                    self.getups.append(now)
                    act = "get_up"
            elif self.settle_due is not None and now >= self.settle_due and not self.running:
                self.settle_due = None
                act = "settle" if not lying else None
            else:
                act = None
        if act == "help":
            self.say("I fell over and can't get up by myself. Please help me up.")
            if self.on_fall:
                self.on_fall("Tony fell and could not get up after two tries; he asked for help. Don't move.")
        elif act == "get_up":
            log.warning("fallen (tilt %s): getting up", st.get("tilt_deg"))
            self.tonyd.stop()
            if self.on_fall:
                self.on_fall("Tony fell over (tilt %s°) and is getting up by himself; check before moving again."
                             % st.get("tilt_deg"))
            self.say("I fell over. Getting up.")
            self.tonyd.run("get_up", 1, wait=False)
        elif act == "settle":
            self.tonyd.run("settle", 1, wait=False)
        return act
