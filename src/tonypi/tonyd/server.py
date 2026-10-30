"""tonyd: the body daemon. The only process that owns the controller board (/dev/ttyAMA0) and the camera.

Brains (the voice agent, the MCP server, scripts) talk to it over a Unix socket with JSON lines:

  request  {"op": "run", "skill": "wave", "count": 1, "wait": true, "wait_s": 180, "rid": 7}
  reply    {"rid": 7, "ok": true, "job": {"id": 3, "skill": "wave", "state": "done", "said": [], ...}}
  event    {"event": "job" | "warn", ...}          (only to connections that sent {"op": "subscribe"})

ops: ping, skills, run, stop, state, head, snapshot, scan, burst, relax, subscribe,
     and for brains that write their own moves (studio.py): joints, pose, play, torque, motions, motion_get,
     motion_save, motion_delete, skill_save, skill_delete, calibration, servo_calibration, servo_calibrate;
     tether {on, max_steps, reset} (walking-step budget while plugged into the charger).
One body job runs at a time and the newest preempts. What stays here whatever a brain does: stop, servo temperature
(body.py), torque off whenever idle, and low-battery / overheating / camera lost-or-back warnings. Falls are the
brain's job: tonyd reports tilt and `lying`, and only stops a job for tilting if it asks (`guard_tilt_deg`).
"""

from __future__ import annotations

import argparse
import itertools
import json
import logging
import os
import queue
import socket
import threading
import time
from dataclasses import dataclass, field, fields
from pathlib import Path

from tonypi.tonyd import SOCK
from tonypi.tonyd.body import LOW_BATTERY_MV, Body
from tonypi.tonyd.camera import Camera
from tonypi.tonyd.skills import MAX_COUNT, SKILLS
from tonypi.tonyd.studio import parse_frames

log = logging.getLogger("tonypi.tonyd")
SNAP_DIR = Path(os.environ.get("TONYD_SNAP_DIR", "/run/tonypi"))
# Tethered to the charger (the board only reports voltage, so this is a setting, not detected): walking steps
# since the tether was set or reset are capped so the cable isn't dragged. Turns and in-place moves don't count.
TETHER_DEFAULT = {"on": True, "max_steps": 10}
WALKING = {"walk_forward", "walk_backward", "step_left", "step_right"}


def is_walking(skill: str) -> bool:
    return skill in WALKING or "walk" in skill  # user gaits like walk_forward_own count too


IDLE_RELAX_S = 1.5  # torque off this long after the last skill (a brain's next step usually comes sooner)
WATCH_S = 20
IDLE_HOT_C = 80
WARN_EVERY_S = 300
KEEP_SNAPSHOTS = 40  # scan + burst can take a dozen at once

# head: PWM pulse = centre + degrees * US_PER_DEG (500-2500 us ~ 180 deg). Pan + = left, tilt + = up.
PAN, TILT = 2, 1
HEAD_CENTRE = {PAN: 1466, TILT: 1500}
US_PER_DEG = 2000 / 180
PAN_LIMIT, TILT_LIMIT = (-60.0, 60.0), (-40.0, 25.0)


@dataclass
class Job:
    id: int
    skill: str
    count: int
    state: str = "queued"  # queued -> running -> done | failed | cancelled
    said: list[str] = field(default_factory=list)
    error: str | None = None
    started: float | None = None
    seconds: float | None = None
    hold_s: float = 0.0  # keep torque on this long after the job (a pose being held) before going limp
    guard_tilt_deg: float | None = None  # opt-in: stop the job if Tony tilts past this (falls are the brain's job)
    max_tilt_deg: float | None = None  # how far Tony leaned during the job (judging a new gait)
    max_tilt_raw_deg: float | None = None  # the same from the accelerometer alone (spikes on fast moves)
    yaw_deg: float | None = None  # how far he turned during the job, from the gyro (+ = left)
    rot_deg: list[float] | None = None  # gyro integral on each axis during the job (x, y, z)
    frames: list | None = field(default=None, repr=False)  # a brain's own keyframes ("_play")
    finished: threading.Event = field(default_factory=threading.Event, repr=False)

    def public(self) -> dict:
        return {f.name: getattr(self, f.name) for f in fields(self) if f.name not in ("finished", "frames")}


class Tonyd:
    def __init__(self, body=None, camera=None) -> None:
        self.body = body if body is not None else Body()
        self.camera = camera
        self.lock = threading.Lock()  # held while a skill runs, and by the watchdog's bus reads
        self.queue: queue.Queue[Job] = queue.Queue(maxsize=1)
        self.current: Job | None = None
        self.ids = itertools.count(1)
        self.subs: dict[socket.socket, threading.Lock] = {}
        self.subs_lock = threading.Lock()
        self.last_skill_end = 0.0
        self.torque_maybe_on = True
        self.hold_s = 0.0  # the last job asked to hold its pose this long
        self.walked = 0  # walking steps since the tether was set / reset
        self.last_warn = 0.0
        self.cached: dict = {}
        self.cached_t = 0.0
        self.head_deg = {"pan": 0.0, "tilt": 0.0}
        self._snap_ids = itertools.count(1)
        if camera is not None:
            camera.on_change = self._camera_changed

    # ---- events ---------------------------------------------------------------------------------------------

    def emit(self, event: str, **kw) -> None:
        line = (json.dumps({"event": event, "t": round(time.time(), 2), **kw}) + "\n").encode()
        with self.subs_lock:
            subs = list(self.subs.items())
        for conn, wlock in subs:
            try:
                with wlock:
                    conn.sendall(line)
            except OSError:
                with self.subs_lock:
                    self.subs.pop(conn, None)

    # ---- skills ---------------------------------------------------------------------------------------------

    def skills(self) -> dict[str, dict]:
        """Every skill a brain can run: the built-in ones plus the studio's (defaults + the user's)."""
        out = {k: {"description": v, "source": "builtin"} for k, v in SKILLS.items()}
        out.update(self.body.studio.skills())
        return out

    def tether(self, on: bool | None = None, max_steps: int | None = None, reset: bool = False) -> dict:
        path = self.body.studio.user_dir / "tether.json"
        t = {**TETHER_DEFAULT, **(json.loads(path.read_text()) if path.exists() else {})}
        if on is not None or max_steps is not None:
            if on is not None:
                t["on"] = bool(on)
            if max_steps is not None:
                t["max_steps"] = max(1, min(100, int(max_steps)))
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(t) + "\n")
            reset = True
        if reset:
            self.walked = 0
        return {**t, "walked": self.walked}

    def submit(self, skill: str, count: int = 1, frames=None, hold_s: float = 0.0,
               guard_tilt_deg: float | None = None) -> Job:
        if frames is not None:
            frames, skill = [{"ms": f.ms, "pos": f.pos} for f in parse_frames(frames)], "_play"
        elif skill not in (known := self.skills()):
            raise ValueError(f"unknown skill {skill!r}; known: {', '.join(known)}")
        steps = max(1, min(MAX_COUNT, int(count)))
        tether = self.tether()
        if tether["on"] and is_walking(skill):
            if self.walked + steps > tether["max_steps"]:
                raise ValueError(f"tethered to the charger: {self.walked} of {tether['max_steps']} walking steps used. "
                                 "Turn and walk back, then tether reset; or tether off once unplugged")
            self.walked += steps
        job = Job(next(self.ids), skill, max(1, min(MAX_COUNT, int(count))),
                  hold_s=max(0.0, min(120.0, float(hold_s or 0))), frames=frames,
                  guard_tilt_deg=None if guard_tilt_deg is None else max(5.0, min(120.0, float(guard_tilt_deg))))
        self.body.cancel()  # the newest command preempts whatever is running
        self._drop_queued()
        self.queue.put(job)
        self.emit("job", **job.public())
        return job

    def stop(self) -> None:
        self._drop_queued()
        self.body.cancel()

    def _drop_queued(self) -> None:
        try:
            old = self.queue.get_nowait()
        except queue.Empty:
            return
        self._finish(old, "cancelled")

    def _finish(self, job: Job, state: str, error: str | None = None) -> None:
        job.state, job.error = state, error
        if job.started is not None:
            job.seconds = round(time.monotonic() - job.started, 2)
        job.finished.set()
        self.emit("job", **job.public())

    def _worker(self) -> None:
        while True:
            job = self.queue.get()
            with self.lock:
                self.current = job
                self.body.cancelled.clear()
                job.state, job.started = "running", time.monotonic()
                heading0 = getattr(self.body, "heading_deg", None)
                rot0 = list(getattr(self.body, "rot_deg", []))
                self.emit("job", **job.public())
                log.info("skill %s x%d (job %d)", job.skill, job.count, job.id)
                try:
                    if job.skill == "settle":
                        self.body.settle()
                        ok = True
                    elif job.frames is not None:
                        ok = self.body.play_frames(job.frames, job.said.append, job.guard_tilt_deg)
                    else:
                        ok = self.body.run(job.skill, job.count, job.said.append, job.guard_tilt_deg)
                    state = "done" if ok else ("cancelled" if self.body.cancelled.is_set() else "failed")
                    job.max_tilt_deg = getattr(self.body, "max_tilt", None)  # before _finish: waiters reply then
                    job.max_tilt_raw_deg = getattr(self.body, "max_tilt_raw", None)
                    if heading0 is not None:
                        job.yaw_deg = round(self.body.heading_deg - heading0, 1)
                        job.rot_deg = [round(a - b, 1) for a, b in zip(self.body.rot_deg, rot0)]
                    self._finish(job, state)
                except Exception as e:
                    log.exception("skill %s failed", job.skill)
                    self._finish(job, "failed", f"{type(e).__name__}: {e}")
                finally:
                    self.hold_s = job.hold_s
                    self.current = None
                    self.last_skill_end = time.monotonic()
                    self.torque_maybe_on = True

    # ---- state ----------------------------------------------------------------------------------------------

    def state(self, fresh: bool = False) -> dict:
        if fresh and self.lock.acquire(timeout=0.5):  # while a skill runs, the cached reading has to do
            try:
                self._refresh()
            finally:
                self.lock.release()
        b = self.body
        tilt = b.tilt_deg()
        imu = getattr(b.board, "imu", None)
        cur = self.current
        return {
            "busy": cur is not None or not self.queue.empty(),
            "job": cur.public() if cur else None,
            "tilt_deg": None if tilt is None else round(tilt, 1),
            "tilt_raw_deg": None if getattr(b, "tilt_raw_deg", None) is None or b.tilt_raw_deg() is None
            else round(b.tilt_raw_deg(), 1),
            "heading_deg": None if getattr(b, "heading_deg", None) is None else round(b.heading_deg, 1),
            "lying": b.lying(),
            "asleep": b.asleep,
            "battery_mv": b.board.battery_mv,
            "imu_age_s": None if imu is None else round(time.monotonic() - imu.t, 2),
            "torque_on": self.cached.get("torque_on"),
            "temps": self.cached.get("temps", {}),
            "hottest": self.cached.get("hottest"),
            "sensors_age_s": round(time.monotonic() - self.cached_t, 1) if self.cached_t else None,
            "head_deg": dict(self.head_deg),
            "camera": self.camera.status() if self.camera else {"ok": False, "device": None},
            "tether": self.tether(),
        }

    def _refresh(self) -> dict:
        st = self.body.status()  # 32 bus reads, ~0.1 s
        self.cached, self.cached_t = st, time.monotonic()
        return st

    def _watchdog(self) -> None:
        next_check = time.monotonic() + 3
        while True:
            time.sleep(0.25)
            now = time.monotonic()
            idle = self.current is None and self.queue.empty()
            # idle = torque off, whatever pose it ended in (Tony carries no load and holds the pose limp). Ending in a
            # stable pose (run "settle", or hold_s) is the brain's call: going limp mid-step can tip him over.
            if idle and self.torque_maybe_on and now - self.last_skill_end >= max(IDLE_RELAX_S, self.hold_s):
                if self.lock.acquire(blocking=False):
                    try:
                        if self.current is None and self.queue.empty():
                            self.body.relax()
                            self.torque_maybe_on = False
                    finally:
                        self.lock.release()
            # the 20 s check unloads torque left on by others, never earlier than the idle delay above: unloading
            # right as a step ends drops Tony mid-sway (2026-10-29: fell on its back after turn_left)
            if now < next_check or not idle or now - self.last_skill_end < max(IDLE_RELAX_S, self.hold_s):
                continue
            next_check = now + WATCH_S
            if not self.lock.acquire(blocking=False):
                continue
            try:
                st = self._refresh()
                if st["torque_on"]:  # something left torque on (another tool, a crash mid-skill)
                    self.body.relax()
            except Exception:
                log.exception("watchdog status failed")
                continue
            finally:
                self.lock.release()
            if now - self.last_warn < WARN_EVERY_S:
                continue
            msg = None
            if st["torque_on"] and st["hottest"] and st["hottest"][2] >= IDLE_HOT_C:
                _, joint, temp = st["hottest"]
                msg = f"My {joint} was at {temp} degrees, so I switched my motors off to cool down."
            elif st["battery_mv"] and st["battery_mv"] < LOW_BATTERY_MV:
                msg = f"My battery is low, at {st['battery_mv'] / 1000:.1f} volts. Please charge me."
            if msg:
                self.last_warn = now
                log.warning(msg)
                self.emit("warn", text=msg)

    def _camera_changed(self, ok: bool) -> None:
        text = "Camera reconnected." if ok else "Camera disconnected."
        (log.info if ok else log.warning)(text)
        self.emit("warn", text=text, kind="camera")

    # ---- head, camera ---------------------------------------------------------------------------------------

    def head(self, pan_deg: float = 0.0, tilt_deg: float = 0.0, ms: int = 400) -> dict:
        pan = max(PAN_LIMIT[0], min(PAN_LIMIT[1], float(pan_deg)))
        tilt = max(TILT_LIMIT[0], min(TILT_LIMIT[1], float(tilt_deg)))
        self.body.board.head(max(100, min(3000, int(ms))), {
            PAN: round(HEAD_CENTRE[PAN] + pan * US_PER_DEG),
            TILT: round(HEAD_CENTRE[TILT] + tilt * US_PER_DEG),
        })
        self.head_deg = {"pan": pan, "tilt": tilt}
        return dict(self.head_deg)

    def snapshot(self, newer_than: float | None = None) -> dict:
        if self.camera is None:
            raise RuntimeError("camera disabled")
        n = next(self._snap_ids)
        path = SNAP_DIR / f"snap-{n:06d}.jpg"
        age = self.camera.snapshot(path, newer_than=newer_than)
        if age is None:
            raise RuntimeError("no camera frame (is the camera plugged in?)")
        old = SNAP_DIR / f"snap-{n - KEEP_SNAPSHOTS:06d}.jpg"
        old.unlink(missing_ok=True)
        return {"path": str(path), "frame_age_s": round(age, 3), "width": self.camera.width,
                "height": self.camera.height, "head_deg": dict(self.head_deg)}

    def scan(self, pans=(60, 0, -60), tilt_deg: float | None = None, settle_s: float = 0.5) -> dict:
        """Look around with the head only: one photo per pan angle (+ = left), then the head goes back."""
        back = dict(self.head_deg)
        tilt = back["tilt"] if tilt_deg is None else float(tilt_deg)
        views = []
        try:
            for pan in list(pans)[:7]:
                self.head(pan, tilt, ms=350)
                moved = time.monotonic() + 0.35 + max(0.1, min(2.0, float(settle_s)))
                time.sleep(moved - time.monotonic())
                views.append({**self.snapshot(newer_than=moved), "pan_deg": self.head_deg["pan"],
                              "tilt_deg": self.head_deg["tilt"]})
        finally:
            self.head(back["pan"], back["tilt"], ms=350)
        b = self.body
        return {"views": views, "heading_deg": None if getattr(b, "heading_deg", None) is None else round(b.heading_deg, 1)}

    def burst(self, frames: int = 4, seconds: float = 2.0) -> dict:
        """A short "video": `frames` photos spread over `seconds` (e.g. while a skill started with wait=false runs)."""
        frames = max(2, min(12, int(frames)))
        gap = max(0.2, min(10.0, float(seconds))) / (frames - 1)
        shots, t0 = [], time.monotonic()
        for i in range(frames):
            time.sleep(max(0.0, t0 + i * gap - time.monotonic()))
            shot = self.snapshot()
            shot["t_s"] = round(time.monotonic() - t0, 2)
            shots.append(shot)
        return {"frames": shots}

    # ---- socket server --------------------------------------------------------------------------------------

    def handle(self, msg: dict, reply) -> None:
        """Runs one request; `reply(dict)` sends the answer (from another thread for run+wait)."""
        op = msg.get("op")
        if op == "ping":
            reply({"pong": True})
        elif op == "skills":
            details = self.skills()
            reply({"skills": {k: v.get("description", "") for k, v in details.items()}, "details": details,
                   "max_count": MAX_COUNT})
        elif op in ("run", "play", "pose"):
            if op == "pose":
                msg = {**msg, "frames": [{"ms": msg.get("ms", 500), "pos": msg.get("positions") or {}}]}
            job = self.submit(str(msg.get("skill", "")), msg.get("count", 1),
                              frames=msg.get("frames") if op != "run" else None, hold_s=msg.get("hold_s", 0),
                              guard_tilt_deg=msg.get("guard_tilt_deg"))
            if not msg.get("wait"):
                reply({"job": job.public()})
                return
            timeout = float(msg.get("wait_s", 180))

            def waiter() -> None:
                job.finished.wait(timeout)
                reply({"job": job.public(), "timed_out": not job.finished.is_set()})

            threading.Thread(target=waiter, daemon=True).start()
        elif op == "stop":
            self.stop()
            reply({"stopped": True})
        elif op == "state":
            reply({"state": self.state(bool(msg.get("fresh")))})
        elif op == "head":
            reply({"head_deg": self.head(msg.get("pan_deg", 0), msg.get("tilt_deg", 0), msg.get("ms", 400))})
        elif op == "snapshot":
            reply(self.snapshot())
        elif op == "scan":
            reply(self.scan(msg.get("pans") or (60, 0, -60), msg.get("tilt_deg"), msg.get("settle_s", 0.5)))
        elif op == "burst":
            reply(self.burst(msg.get("frames", 4), msg.get("seconds", 2.0)))
        elif op == "relax":
            self.stop()
            with self.lock:
                self.body.relax()
                self.torque_maybe_on = False
            reply({"relaxed": True})
        elif op == "tether":
            reply({"tether": self.tether(msg.get("on"), msg.get("max_steps"), bool(msg.get("reset")))})
        elif op == "joints":
            got = self.lock.acquire(timeout=0.5)  # reading mid-motion is fine, just not racing the watchdog
            try:
                reply(self.body.joints())
            finally:
                if got:
                    self.lock.release()
        elif op == "torque":  # e.g. off on one leg to pose it by hand, then `joints` to record the pose
            on = bool(msg.get("on"))
            with self.lock:
                self.body.torque(msg.get("ids") or range(1, 17), on)
                if on:
                    self.torque_maybe_on, self.last_skill_end = True, time.monotonic()
                    self.hold_s = max(0.0, min(120.0, float(msg.get("hold_s", 0))))
            reply({"torque": on})
        else:
            reply(self._studio_op(op, msg))

    def _studio_op(self, op: str, msg: dict) -> dict:
        st = self.body.studio
        if op == "motions":
            return {"motions": st.list_motions()}
        if op == "motion_get":
            return st.get_motion(msg.get("name", ""))
        if op == "motion_save":
            return st.save_motion(msg.get("name", ""), msg.get("frames"), msg.get("description", ""))
        if op == "motion_delete":
            return st.delete_motion(msg.get("name", ""))
        if op == "skill_save":
            return st.save_skill(msg.get("name", ""), msg.get("motions"), msg.get("description", ""))
        if op == "skill_delete":
            return st.delete_skill(msg.get("name", ""))
        if op == "servo_calibration":
            with self.lock:
                return {"servos": self.body.servo_calibration(msg.get("ids") or range(1, 17))}
        if op == "servo_calibrate":
            # the servos' own EEPROM offset / angle limits: only to fix a bad calibration, so a brain must say why
            reason = str(msg.get("reason") or "").strip()
            if len(reason) < 10:
                raise ValueError("servo_calibrate changes the servo itself; only use it to fix a bad calibration and "
                                 "pass reason='what is wrong and how you know'")
            sid = int(msg.get("id", 0))
            with self.lock:
                got = self.body.servo_calibrate(sid, msg.get("offset"), msg.get("limits"), bool(msg.get("save")))
            entry = {"t": time.strftime("%Y-%m-%dT%H:%M:%S"), "id": sid, "offset": msg.get("offset"),
                     "limits": msg.get("limits"), "save": bool(msg.get("save")), "reason": reason, "now": got}
            log.warning("servo_calibrate %s", entry)
            st.user_dir.mkdir(parents=True, exist_ok=True)
            with open(st.user_dir / "servo-calibration-log.jsonl", "a") as f:
                f.write(json.dumps(entry) + "\n")
            return {"servo": sid, **got}
        if op == "calibration":
            if msg.get("trim") is not None or msg.get("limits") is not None:
                st.set_calibration(msg.get("trim"), msg.get("limits"))
            return {"calibration": self.body.reload_calibration()}
        raise ValueError(f"unknown op {op!r}")

    def _client(self, conn: socket.socket) -> None:
        wlock = threading.Lock()

        def send(obj: dict) -> None:
            try:
                with wlock:
                    conn.sendall((json.dumps(obj) + "\n").encode())
            except OSError:
                pass

        try:
            for line in conn.makefile("r", encoding="utf-8"):
                if not line.strip():
                    continue
                try:
                    msg = json.loads(line)
                    rid = msg.get("rid")
                    if msg.get("op") == "subscribe":
                        with self.subs_lock:
                            self.subs[conn] = wlock
                        send({"rid": rid, "ok": True, "subscribed": True})
                        continue
                    self.handle(msg, lambda r, rid=rid: send({"rid": rid, "ok": True, **r}))
                except Exception as e:
                    send({"rid": msg.get("rid") if isinstance(msg, dict) else None, "ok": False,
                          "error": f"{e}" if isinstance(e, (ValueError, RuntimeError)) else f"{type(e).__name__}: {e}"})
        except OSError:
            pass
        finally:
            with self.subs_lock:
                self.subs.pop(conn, None)
            conn.close()

    def serve(self, path: str = SOCK) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.unlink(missing_ok=True)
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(str(p))
        os.chmod(p, 0o660)
        srv.listen(16)
        log.info("listening on %s", p)
        while True:
            conn, _ = srv.accept()
            threading.Thread(target=self._client, args=(conn,), daemon=True).start()

    def start(self) -> "Tonyd":
        threading.Thread(target=self._worker, name="skills", daemon=True).start()
        threading.Thread(target=self._watchdog, name="watchdog", daemon=True).start()
        return self


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--no-camera", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    camera = None if args.no_camera else Camera()
    d = Tonyd(Body(), camera).start()  # wires camera.on_change before the camera thread runs
    if camera:
        camera.start()
    d.head(0, 0)
    try:
        d.serve()
    finally:
        d.body.close()


if __name__ == "__main__":
    main()
