"""Runs skills and brain-written motions on the robot with safety guards. Owns the Board (/dev/ttyAMA0).

Guards never depend on an LLM. Everyday library jobs use bounded fall recovery here; explicit learning jobs retain
opt-in guards and measured learning-runner recovery. Stop, heat, battery and idle torque rules apply to both.
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
from tonypi.motion.library import Motion, Player
from tonypi.tonyd.skills import SKILLS
from tonypi.tonyd.studio import Studio, parse_frames

log = logging.getLogger(__name__)

HOT_SKIP_C = 81  # don't start a motion if a servo it uses is this hot
HOT_STOP_C = 83  # stop a running motion (servo self-protects at 85)
FALL_HOLD_S = 0.3  # an opt-in tilt guard (guard_tilt_deg) trips after this long past its angle
LYING_TILT_DEG = 60
# Heading from the gyro: upright, gravity is along -X, so turning about the vertical is the X rate (deg/s).
# The bias is learned while no motion plays and the gyro is still; integration runs the rest of the time (a step
# keeps rotating after its keyframes end). YAW_SIGN makes a left turn positive.
YAW_AXIS, YAW_SIGN = 0, -1.0  # checked 2026-10-29 against camera shift: matches within ~1 deg (HFOV ~60 deg)
GYRO_BIAS0 = (-1.05, 0.14, -0.37)  # measured 2026-10-28; refined live
# Tilt = gravity direction from a complementary filter: the gyro carries it through fast moves (arm swings shake the
# torso and spike the accelerometer), the accelerometer pulls it back with time constant ~1 / (100 Hz * TILT_K).
TILT_K, TILT_K_JOLT = 0.01, 0.002  # per sample (tau ~1 s); JOLT when |accel| is far from 1 g (impacts, swings)
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
DEFAULT_SKILLS = {name: {"motions": seq, "description": SKILLS.get(name, "")} for name, seq in SEQUENCES.items()}
DANCES = [f"dance{i}" for i in range(1, 10)]
JOINT_NAMES = {1: "left ankle", 2: "left ankle", 3: "left knee", 4: "left hip", 5: "left hip", 6: "left elbow",
               7: "left shoulder", 8: "left shoulder", 9: "right ankle", 10: "right ankle", 11: "right knee",
               12: "right hip", 13: "right hip", 14: "right elbow", 15: "right shoulder", 16: "right shoulder"}


class Body:
    def __init__(self, device: str = "/dev/ttyAMA0") -> None:
        self.board = Board(device)
        self.player = Player(self.board)
        self.studio = Studio(default_skills=DEFAULT_SKILLS)
        self.reload_calibration()
        self.max_tilt: float | None = None  # highest tilt during the last motion (for judging a new gait)
        self.heading_deg = 0.0  # integrated gyro yaw since start (+ = left); drifts slowly, use differences
        self.rot_deg = [0.0, 0.0, 0.0]  # the same integral on all three gyro axes (gait analysis)
        self._bias = list(GYRO_BIAS0)
        self._imu_t: float | None = None
        self._grav: list[float] | None = None  # filtered gravity direction (unit vector, board axes)
        self.max_tilt_raw: float | None = None
        self.board.imu_listeners.append(self._on_imu)
        self.cancelled = threading.Event()
        self._abort_reason: str | None = None
        self._last_dance: str | None = None
        self.asleep = False
        time.sleep(0.3)
        self.relax()

    def head_command(self, ms: int, pulses: dict[int, int]) -> None:
        """Overridden by tonyd to tag legacy head gestures in the same capture timeline."""
        self.board.head(ms, pulses)

    def close(self) -> None:
        self.board.close()

    def reload_calibration(self) -> dict:
        cal = self.studio.calibration()
        self.player.transform = lambda targets: self.studio.to_servo(cal, targets)
        self._cal = cal
        return cal

    # ---- state ------------------------------------------------------------------------------------------------

    def tilt_deg(self) -> float | None:
        """Tilt from upright (degrees): gyro + accelerometer fused; the raw accelerometer before the first samples."""
        v = getattr(self, "_grav", None)
        if v is None:
            return self.tilt_raw_deg()
        return math.degrees(math.acos(max(-1.0, min(1.0, -v[0]))))

    def _on_imu(self, sample) -> None:
        """Every IMU sample (~100 Hz, board thread): learn the gyro bias while still, integrate heading."""
        t, prev = sample.t, self._imu_t
        self._imu_t = t
        g = sample.gyro
        self._fuse_gravity(sample, None if prev is None else t - prev)
        if self.player.playing is None and all(abs(g[i] - self._bias[i]) < 2.0 for i in range(3)):
            for i in range(3):  # still: what the gyro says now is bias
                self._bias[i] += 0.01 * (g[i] - self._bias[i])
            return
        if prev is not None and 0 < t - prev < 0.1:
            for i in range(3):
                self.rot_deg[i] += (sample.gyro[i] - self._bias[i]) * (t - prev)
            self.heading_deg += YAW_SIGN * (sample.gyro[YAW_AXIS] - self._bias[YAW_AXIS]) * (t - prev)

    def _fuse_gravity(self, sample, dt: float | None) -> None:
        a = sample.accel
        n = math.sqrt(sum(x * x for x in a))
        if n < 0.2:
            return
        an = [x / n for x in a]
        v = self._grav
        if v is None or dt is None or not 0 < dt < 0.1:
            self._grav = an
            return
        w = [math.radians(sample.gyro[i] - self._bias[i]) for i in range(3)]
        # a world-fixed vector seen from the rotating body: dv/dt = -w x v
        cx, cy, cz = w[1] * v[2] - w[2] * v[1], w[2] * v[0] - w[0] * v[2], w[0] * v[1] - w[1] * v[0]
        pred = [v[0] - cx * dt, v[1] - cy * dt, v[2] - cz * dt]
        k = TILT_K if abs(n - 1.0) < 0.15 else TILT_K_JOLT
        mix = [(1 - k) * p + k * q for p, q in zip(pred, an)]
        m = math.sqrt(sum(x * x for x in mix)) or 1.0
        self._grav = [x / m for x in mix]

    def tilt_raw_deg(self) -> float | None:
        """Tilt from the accelerometer alone (spikes during fast moves)."""
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

    def run(self, action: str, count: int, say: Callable[[str], None], guard_tilt: float | None = None) -> bool:
        """Run one skill. Returns False if it was refused, cancelled or aborted by a guard. guard_tilt (degrees, opt-in)
        stops it if Tony tilts past that angle for FALL_HOLD_S; nothing else reacts to tilt."""
        if self.cancelled.is_set():
            return False
        if action == "sleep":
            return self.sleep(say)
        if action not in HEAD_POSES and action not in ("nod", "shake_head"):
            self.asleep = False  # any body move re-enables torque; getting up first is the brain's call
        if action in HEAD_POSES:
            self.head_command(500, HEAD_POSES[action])
            return self._sleep(0.6)
        if action in ("nod", "shake_head"):
            ch, a, b = (TILT, 1650, 1350) if action == "nod" else (PAN, 1700, 1230)
            for _ in range(2):
                for p in (a, b):
                    self.head_command(220, {ch: p})
                    if not self._sleep(0.25):
                        return False
            self.head_command(250, {ch: HEAD_CENTRE[ch]})
            return self._sleep(0.3)
        if action == "get_up" or (action == "stand" and self.lying()):  # stand_slow alone fails from the floor
            if not self.lying():
                return self._motions(["stand_slow"], say)
            s = self.board.imu
            # Board-axis observation: gravity has positive Z face-down and negative Z on the back.
            # This legacy learning path keeps its existing back default when no IMU sample is available.
            name = "stand_up_front" if s is not None and s.accel[2] > 0 else "stand_up_back"
            return self._motions([name, "stand_slow"], say)
        if action == "dance":
            name = random.choice([d for d in DANCES if d != self._last_dance])
            self._last_dance = name
            return self._motions([name], say, guard_tilt)
        skill = self.studio.skills().get(action)
        if skill is not None:
            names = []
            for n in skill["motions"]:
                names += [n[1:]] * count if n.startswith("*") else [n]
            return self._motions(names, say, guard_tilt)
        log.warning("unknown action %s", action)
        return False

    def _everyday_preflight(self) -> None:
        """Measured runtime conditions, checked before each segment and before any recovery."""
        if self.cancelled.is_set():
            raise ValueError("motion cancelled; no recovery")
        imu = self.board.imu
        if imu is None or not 0 <= time.monotonic() - imu.t <= 1:
            raise ValueError("fresh IMU required for everyday movement")
        if self.board.battery_mv is None or self.board.battery_mv < 10_900:
            raise ValueError("battery must be measured and >=10.9 V")
        temps = self.temps()
        if any(temps.get(i) is None for i in range(1, 17)) or max(temps.values()) >= 70:
            raise ValueError("all servo temperatures must be measured and below 70 C")

    def _recover_everyday(self, say: Callable[[str], None]) -> bool:
        from tonypi.motion.catalog import resolve_skill

        self._everyday_preflight()
        tilt, imu = self.tilt_deg(), self.board.imu
        if tilt is None:
            raise ValueError("unknown orientation; cannot select get-up")
        if tilt <= 20:
            return True
        if not self.lying() or abs(imu.accel[2]) < 0.35:
            raise ValueError("ambiguous/sideways orientation; no verified get-up fits")
        name = "get_up_front" if imu.accel[2] > 0 else "get_up_back"
        motions = resolve_skill(name, calibration=self.studio.calibration(), count=1, user_dir=self.studio.user_dir)
        self._abort_reason = None
        if not self._everyday_motions(motions, say):
            return False
        # Give the continuous fused IMU a bounded interval to follow the ending posture; never clear cancellation.
        if not self._sleep(0.4):
            return False
        return not self.cancelled.is_set() and self.tilt_deg() is not None and self.tilt_deg() <= 20

    def _verify_everyday_calibration(self) -> None:
        from tonypi.motion.catalog import manifest

        expected = manifest()["hardware_calibration_snapshot"]
        measured = {str(s): value for s, value in self.servo_calibration().items()}
        if measured != expected:
            raise ValueError("servo EEPROM calibration differs from library audit; validate again before moving")

    def run_everyday(self, action: str, count: int, say: Callable[[str], None]) -> bool:
        self._everyday_active = True
        self._everyday_peaks = [None, None]
        self.everyday_recoveries, self.everyday_completed = 0, 0
        try:
            self._everyday_preflight()
            self._verify_everyday_calibration()
            return self._run_everyday(action, count, say)
        finally:
            self.max_tilt, self.max_tilt_raw = self._everyday_peaks
            self._everyday_active = False

    def _everyday_motions(self, motions, say, guard_tilt=None) -> bool:
        try:
            return self._motions(motions, say, guard_tilt)
        finally:
            for i, value in enumerate((getattr(self, "max_tilt", None), getattr(self, "max_tilt_raw", None))):
                peak = self._everyday_peaks[i]
                if value is not None and (peak is None or value > peak):
                    self._everyday_peaks[i] = value

    def _run_everyday(self, action: str, count: int, say: Callable[[str], None]) -> bool:
        """Immutable library execution. One fall recovery per job; never resumes after stop or a thermal failure.

        A completed segment is not repeated after a fall. An interrupted segment is retried once; repeated steps
        before/after it are not replayed. Recovery is limited to this requested job, never an idle reflex.
        """
        from tonypi.motion.catalog import resolve_skill

        self._everyday_preflight()
        if action in ("get_up", "get_up_front", "get_up_back"):
            if action != "get_up":
                z = self.board.imu.accel[2]
                if not self.lying() or (action == "get_up_front") != (z > 0):
                    raise ValueError("get-up precondition does not match measured orientation")
            return self._recover_everyday(say)
        motions = resolve_skill(action, calibration=self.studio.calibration(), count=count, user_dir=self.studio.user_dir)
        expected_floor = action == "lie_down"
        if self.lying():
            if not self._recover_everyday(say):
                return False
            recovered = True
            self.everyday_recoveries += 1
        else:
            tilt = self.tilt_deg()
            if tilt is None or tilt > 20:
                raise ValueError("library motion requires an upright standing start (tilt <=20 degrees)")
            recovered = False
        self.asleep = False
        for motion in motions:
            self._everyday_preflight()
            self._abort_reason = None
            ok = self._everyday_motions([motion], say, None if expected_floor else 40)
            # Cancellation always wins; recovery must never clear the cancellation event or stop latch.
            if self.cancelled.is_set():
                return False
            fell = (not expected_floor and (self._abort_reason == "tilt" or (ok and self.lying())))
            if fell and not recovered:
                say("I fell; trying one library get-up before continuing.")
                if not self._recover_everyday(say):
                    return False
                recovered = True
                self.everyday_recoveries += 1
                if not ok:  # retry only this unfinished segment
                    self._everyday_preflight()
                    self._abort_reason = None
                    ok = self._everyday_motions([motion], say, 40)
                else:
                    self.everyday_completed += 1
                    continue
            if not ok or (not expected_floor and self.lying()):
                return False
            self.everyday_completed += 1
        return not self.cancelled.is_set()

    def relax(self) -> None:
        """All bus servo torque off. Safe in any pose: nothing is carried, and the next move() turns it back on."""
        self.board.torque(range(1, 17), on=False)
        log.info("relaxed: servo torque off")

    def sleep(self, say: Callable[[str], None]) -> bool:
        """Lie down, then switch all servo torque off (they cool down; the agent keeps listening)."""
        lay = self.lying() or self._motions(["lie_down"], say)
        self.relax()  # even if lying down was refused or interrupted
        if not lay:
            return False
        self.board.rgb([(i, 0, 0, 0) for i in (1, 2)])
        self.asleep = True
        return True

    def settle(self) -> None:
        """Back to a neutral stand after body motions (not when lying or cancelled)."""
        if not self.cancelled.is_set() and not self.lying() and not self.asleep:
            log.info("settle: stand_slow")
            self.player.play(self.studio.load_motion("stand_slow"))

    # ---- internals --------------------------------------------------------------------------------------------

    def _sleep(self, s: float) -> bool:
        return not self.cancelled.wait(s)

    def play_frames(self, frames, say: Callable[[str], None], guard_tilt: float | None = None) -> bool:
        """A brain's own keyframes (not saved), under the same guards as any skill."""
        return self._motions([Motion("adhoc", parse_frames(frames))], say, guard_tilt)

    def joints(self) -> dict:
        """Bus servo positions, as raw servo counts and in motion space (raw minus the calibration trim)."""
        raw = self.board.read_positions(range(1, 17))
        trim = self._cal["trim"]
        return {"raw": raw, "pos": {i: None if p is None else p - trim.get(i, 0) for i, p in raw.items()}}

    def servo_calibration(self, ids=range(1, 17)) -> dict:
        """What the servos themselves store (EEPROM): offset (-125..125) and angle limits [lo, hi]."""
        out = {}
        for i in ids:
            lim = self.board.bus_read(i, BusCmd.READ_ANGLE_LIMIT)
            out[i] = {"offset": self.board.bus_read(i, BusCmd.READ_OFFSET), "limits": None if lim is None else list(lim)}
        return out

    def servo_calibrate(self, sid: int, offset: int | None = None, limits: list[int] | None = None,
                        save: bool = False) -> dict:
        if not 1 <= sid <= 16:
            raise ValueError("servo ids 1..16")
        if offset is not None:
            if not -125 <= int(offset) <= 125:
                raise ValueError("offset: -125..125")
            self.board.set_offset(sid, int(offset), save=save)  # save=True writes the servo's EEPROM
        if limits is not None:
            lo, hi = int(limits[0]), int(limits[1])
            if not 0 <= lo < hi <= 1000:
                raise ValueError("limits: 0 <= lo < hi <= 1000")
            self.board.set_angle_limit(sid, lo, hi)  # the servo keeps these (EEPROM)
        return self.servo_calibration([sid])[sid]

    def torque(self, ids, on: bool) -> None:
        self.board.torque([i for i in ids if 1 <= int(i) <= 16], on=on)

    def _motions(self, names: list, say: Callable[[str], None], guard_tilt: float | None = None) -> bool:
        motions = [n if isinstance(n, Motion) else self.studio.load_motion(n) for n in names]
        ids = sorted({i for m in motions for f in m.frames for i in f.pos if i <= 16})
        hot = {i: t for i, t in self.temps(ids).items() if t is not None and t >= HOT_SKIP_C}
        if hot:
            joint = JOINT_NAMES[max(hot, key=hot.get)]
            log.warning("skip %s: hot servos %s", names, hot)
            say(f"My {joint} is too hot, at {max(hot.values())} degrees. I need to cool down first.")
            return False
        self._abort_reason = None
        self.max_tilt = self.max_tilt_raw = None
        done = threading.Event()
        watcher = threading.Thread(target=self._watch, args=(ids, guard_tilt, done), daemon=True)
        watcher.start()
        played = True
        try:
            for m in motions:
                if self.cancelled.is_set() or self._abort_reason:
                    break
                log.info("motion %s (%d ms)", m.name, m.duration_ms)
                if self.cancelled.is_set() or not self.player.play(m):
                    played = False
                    break
        finally:
            done.set()
            watcher.join()
        if self._abort_reason == "hot":
            self.relax()
            say("I'm getting too hot, so I stopped and switched my motors off to cool down.")
        elif self._abort_reason == "tilt":
            say(f"I tilted past {guard_tilt:.0f} degrees, so I stopped.")
        return played and not (self.cancelled.is_set() or self._abort_reason)

    def _watch(self, ids: list[int], guard_tilt: float | None, done: threading.Event) -> None:
        next_temp = time.monotonic() + 1.5
        tilted_since = None
        while not done.wait(0.02):
            if self.cancelled.is_set():
                self.player.stop()
                return
            battery = self.board.battery_mv
            if getattr(self, "_everyday_active", False) and (battery is None or battery < 10_900):
                self._abort_reason = "low_battery"
                self.player.stop()
                self.relax()
                return
            t, raw = self.tilt_deg(), self.tilt_raw_deg()
            if t is not None and (self.max_tilt is None or t > self.max_tilt):
                self.max_tilt = round(t, 1)
            if raw is not None and (self.max_tilt_raw is None or raw > self.max_tilt_raw):
                self.max_tilt_raw = round(raw, 1)
            if guard_tilt is not None and t is not None and t > guard_tilt:
                tilted_since = tilted_since or time.monotonic()
                if time.monotonic() - tilted_since >= FALL_HOLD_S:
                    log.warning("tilt guard: %.0f deg > %.0f", t, guard_tilt)
                    self._abort_reason = "tilt"
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
