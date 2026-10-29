"""Runs plan actions on the robot with safety guards. Owns the Board (the only process on /dev/ttyAMA0).

Guards (never LLM-dependent): servo temperature is checked before and during every motion, the IMU tilt stops a
motion that is falling over, cancel() interrupts anything immediately, and locomotion is refused while lying down.
Torque is only on while a motion plays: the robot carries no load, so it holds any pose (standing included) with the
servos off. relax() unloads everything after each command and whenever a servo overheats.
"""

from __future__ import annotations

import logging
import math
import random
import threading
import time
from typing import Callable

from tonypi.hal.board import Board
from tonypi.hal.proto import BusCmd
from tonypi.motion.library import Player, load

log = logging.getLogger(__name__)

HOT_SKIP_C = 81  # don't start a motion if a servo it uses is this hot
HOT_STOP_C = 83  # stop a running motion (servo self-protects at 85)
FALL_TILT_DEG = 70  # dances lean past 50 deg on purpose; a real fall passes 70 within a fraction of a second
FALL_HOLD_S = 0.3
LYING_TILT_DEG = 60
LOW_BATTERY_MV = 10_500

PAN, TILT = 2, 1
HEAD_CENTRE = {TILT: 1500, PAN: 1466}
HEAD_POSES = {
    "look_left": {PAN: 1866}, "look_right": {PAN: 1066}, "look_up": {TILT: 1750}, "look_down": {TILT: 1150},
    "look_center": HEAD_CENTRE,
}

# action -> motion sequence; "*" repeats that motion `count` times
SEQUENCES: dict[str, list[str]] = {
    "wave": ["wave"], "bow": ["bow"], "kung_fu": ["wing_chun"], "twist": ["twist"],
    "punch_left": ["left_uppercut"], "punch_right": ["right_uppercut"],
    "kick_left": ["left_shot_fast"], "kick_right": ["right_shot_fast"],
    "squat": ["squat_down", "squat_up"], "arms_up": ["both_arms_up"], "flap_wings": ["wings_up"],
    "step_in_place": ["*stepping"],
    "walk_forward": ["go_forward_start", "*go_forward", "go_forward_end"],
    "walk_backward": ["back_start", "*back", "back_end"],
    "turn_left": ["*turn_left"], "turn_right": ["*turn_right"],
    "step_left": ["*left_move"], "step_right": ["*right_move"],
    "stand": ["stand_slow"], "lie_down": ["lie_down"],
}
NEEDS_UPRIGHT = set(SEQUENCES) - {"stand", "lie_down"}
DANCES = [f"dance{i}" for i in range(1, 10)]
JOINT_NAMES = {1: "left ankle", 2: "left ankle", 3: "left knee", 4: "left hip", 5: "left hip", 6: "left elbow",
               7: "left shoulder", 8: "left shoulder", 9: "right ankle", 10: "right ankle", 11: "right knee",
               12: "right hip", 13: "right hip", 14: "right elbow", 15: "right shoulder", 16: "right shoulder"}


class Body:
    def __init__(self, device: str = "/dev/ttyAMA0") -> None:
        self.board = Board(device)
        self.player = Player(self.board)
        self.cancelled = threading.Event()
        self._abort_reason: str | None = None
        self._last_dance: str | None = None
        self.asleep = False
        time.sleep(0.3)
        self.relax()

    def close(self) -> None:
        self.board.close()

    # ---- state ------------------------------------------------------------------------------------------------

    def tilt_deg(self) -> float | None:
        s = getattr(self.board, "imu", None)
        if s is None:
            return None
        ax, ay, az = s.accel
        n = math.sqrt(ax * ax + ay * ay + az * az) or 1.0
        return math.degrees(math.acos(max(-1.0, min(1.0, -ax / n))))  # upright: gravity along -X

    def lying(self) -> bool:
        t = self.tilt_deg()
        return t is not None and t > LYING_TILT_DEG

    def temps(self, ids=range(1, 17)) -> dict[int, int | None]:
        return {i: self.board.bus_read(i, BusCmd.READ_TEMP) for i in ids}

    def status(self) -> dict:
        temps = self.temps()
        known = {i: t for i, t in temps.items() if t is not None}
        hottest = max(known, key=known.get) if known else None
        tilt = self.tilt_deg()
        return {
            "tilt_deg": None if tilt is None else round(tilt, 1),
            "lying": self.lying(),
            "asleep": self.asleep,
            "torque_on": sum(self.board.bus_read(i, BusCmd.READ_TORQUE) == 1 for i in range(1, 17)),
            "battery_mv": self.board.battery_mv,
            "temps": temps,
            "hottest": None if hottest is None else (hottest, JOINT_NAMES[hottest], known[hottest]),
        }

    # ---- control ----------------------------------------------------------------------------------------------

    def cancel(self) -> None:
        self.cancelled.set()
        self.player.stop()

    def run(self, action: str, count: int, say: Callable[[str], None]) -> bool:
        """Run one non-speech action. Returns False if it was refused, cancelled or aborted by a guard."""
        if self.cancelled.is_set():
            return False
        if action == "sleep":
            return self.sleep(say)
        if self.asleep and action not in HEAD_POSES and action not in ("nod", "shake_head"):
            self.asleep = False
            if action != "get_up" and not self.run("get_up", 1, say):  # moving re-enables torque
                return False
        if action in HEAD_POSES:
            self.board.head(500, HEAD_POSES[action])
            return self._sleep(0.6)
        if action in ("nod", "shake_head"):
            ch, a, b = (TILT, 1650, 1350) if action == "nod" else (PAN, 1700, 1230)
            for _ in range(2):
                for p in (a, b):
                    self.board.head(220, {ch: p})
                    if not self._sleep(0.25):
                        return False
            self.board.head(250, {ch: HEAD_CENTRE[ch]})
            return self._sleep(0.3)
        if action == "get_up" or (action == "stand" and self.lying()):  # stand_slow alone fails from the floor
            if not self.lying():
                return self._motions(["stand_slow"], say, guard_fall=False)
            s = self.board.imu
            # face-down vs on-back from the old Hiwonder logic (atan2(ax, az): ~0 deg face-down, ~180 on back)
            name = "stand_up_front" if s is not None and s.accel[2] > 0 else "stand_up_back"
            return self._motions([name, "stand_slow"], say, guard_fall=False)
        if action == "dance":
            name = random.choice([d for d in DANCES if d != self._last_dance])
            self._last_dance = name
            return self._motions([name], say)
        if action in SEQUENCES:
            if action in NEEDS_UPRIGHT and self.lying() and not self.run("get_up", 1, say):
                return False
            names = []
            for n in SEQUENCES[action]:
                names += [n[1:]] * count if n.startswith("*") else [n]
            return self._motions(names, say, guard_fall=action != "lie_down")
        log.warning("unknown action %s", action)
        return False

    def relax(self) -> None:
        """All bus servo torque off. Safe in any pose: nothing is carried, and the next move() turns it back on."""
        self.board.torque(range(1, 17), on=False)
        log.info("relaxed: servo torque off")

    def sleep(self, say: Callable[[str], None]) -> bool:
        """Lie down, then switch all servo torque off (they cool down; the agent keeps listening)."""
        lay = self.lying() or self._motions(["lie_down"], say, guard_fall=False)
        self.relax()  # even if lying down was refused or interrupted
        if not lay:
            return False
        self.board.rgb([(i, 0, 0, 0) for i in (1, 2)])
        self.asleep = True
        return True

    def settle(self) -> None:
        """Back to a neutral stand after body motions (not when lying or cancelled)."""
        if not self.cancelled.is_set() and not self.lying() and not self.asleep:
            self.player.play(load("stand_slow"))

    # ---- internals --------------------------------------------------------------------------------------------

    def _sleep(self, s: float) -> bool:
        return not self.cancelled.wait(s)

    def _motions(self, names: list[str], say: Callable[[str], None], guard_fall: bool = True) -> bool:
        motions = [load(n) for n in names]
        ids = sorted({i for m in motions for f in m.frames for i in f.pos if i <= 16})
        hot = {i: t for i, t in self.temps(ids).items() if t is not None and t >= HOT_SKIP_C}
        if hot:
            joint = JOINT_NAMES[max(hot, key=hot.get)]
            log.warning("skip %s: hot servos %s", names, hot)
            say(f"My {joint} is too hot, at {max(hot.values())} degrees. I need to cool down first.")
            return False
        self._abort_reason = None
        done = threading.Event()
        watcher = threading.Thread(target=self._watch, args=(ids, guard_fall, done), daemon=True)
        watcher.start()
        try:
            for m in motions:
                if self.cancelled.is_set() or self._abort_reason:
                    break
                log.info("motion %s (%d ms)", m.name, m.duration_ms)
                if not self.player.play(m):
                    break
        finally:
            done.set()
            watcher.join()
        if self._abort_reason == "hot":
            self.relax()
            say("I'm getting too hot, so I stopped and switched my motors off to cool down.")
        elif self._abort_reason == "fall":
            if self.lying():
                say("Oops, I fell over. Say get up, and I'll try to stand.")
            else:
                say("Whoa, I wobbled, so I stopped.")
                self.player.play(load("stand_slow"))
        return not (self.cancelled.is_set() or self._abort_reason)

    def _watch(self, ids: list[int], guard_fall: bool, done: threading.Event) -> None:
        next_temp = time.monotonic() + 1.5
        tilted_since = None
        while not done.wait(0.02):
            if guard_fall and (t := self.tilt_deg()) is not None and t > FALL_TILT_DEG:
                tilted_since = tilted_since or time.monotonic()
                if time.monotonic() - tilted_since >= FALL_HOLD_S:
                    log.warning("fall guard: tilt %.0f deg", t)
                    self._abort_reason = "fall"
                    self.player.stop()
                    return
            else:
                tilted_since = None
            if time.monotonic() >= next_temp:
                next_temp = time.monotonic() + 1.5
                hot = {i: t for i, t in self.temps(ids).items() if t is not None and t >= HOT_STOP_C}
                if hot:
                    log.warning("thermal stop: %s", hot)
                    self._abort_reason = "hot"
                    self.player.stop()
                    return
