"""tonyd: the body daemon. The only process that owns the controller board (/dev/ttyAMA0) and the camera.

Brains (the voice agent, the MCP server, scripts) talk to it over a Unix socket with JSON lines:

  request  {"op": "run", "skill": "wave", "count": 1, "wait": true, "wait_s": 180, "rid": 7}
  reply    {"rid": 7, "ok": true, "job": {"id": 3, "skill": "wave", "state": "done", "said": [], ...}}
  event    {"event": "job" | "warn", ...}          (only to connections that sent {"op": "subscribe"})

ops: ping, skills, run, stop, state, head, snapshot, relax, subscribe.
One skill runs at a time and the newest preempts. Safety lives here, never in a brain: servo temperature and
fall guards (body.py), torque off whenever idle, and low-battery / overheating / camera lost-or-back warnings.
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

log = logging.getLogger("tonypi.tonyd")
SNAP_DIR = Path(os.environ.get("TONYD_SNAP_DIR", "/run/tonypi"))
IDLE_RELAX_S = 1.5  # torque off this long after the last skill (a brain's next step usually comes sooner)
WATCH_S = 20
IDLE_HOT_C = 80
WARN_EVERY_S = 300
KEEP_SNAPSHOTS = 10

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
    finished: threading.Event = field(default_factory=threading.Event, repr=False)

    def public(self) -> dict:
        return {f.name: getattr(self, f.name) for f in fields(self) if f.name != "finished"}


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

    def submit(self, skill: str, count: int = 1) -> Job:
        if skill not in SKILLS:
            raise ValueError(f"unknown skill {skill!r}; known: {', '.join(SKILLS)}")
        job = Job(next(self.ids), skill, max(1, min(MAX_COUNT, int(count))))
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
                self.emit("job", **job.public())
                log.info("skill %s x%d (job %d)", job.skill, job.count, job.id)
                try:
                    if job.skill == "settle":
                        self.body.settle()
                        ok = True
                    else:
                        ok = self.body.run(job.skill, job.count, job.said.append)
                    state = "done" if ok else ("cancelled" if self.body.cancelled.is_set() else "failed")
                    self._finish(job, state)
                except Exception as e:
                    log.exception("skill %s failed", job.skill)
                    self._finish(job, "failed", f"{type(e).__name__}: {e}")
                finally:
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
            # idle = torque off, whatever pose it ended in (Tony carries no load and holds the pose limp)
            if idle and self.torque_maybe_on and now - self.last_skill_end >= IDLE_RELAX_S:
                if self.lock.acquire(blocking=False):
                    try:
                        if self.current is None and self.queue.empty():
                            self.body.relax()
                            self.torque_maybe_on = False
                    finally:
                        self.lock.release()
            if now < next_check or not idle:
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

    def snapshot(self) -> dict:
        if self.camera is None:
            raise RuntimeError("camera disabled")
        n = next(self._snap_ids)
        path = SNAP_DIR / f"snap-{n:06d}.jpg"
        age = self.camera.snapshot(path)
        if age is None:
            raise RuntimeError("no camera frame (is the camera plugged in?)")
        old = SNAP_DIR / f"snap-{n - KEEP_SNAPSHOTS:06d}.jpg"
        old.unlink(missing_ok=True)
        return {"path": str(path), "frame_age_s": round(age, 3), "width": self.camera.width,
                "height": self.camera.height, "head_deg": dict(self.head_deg)}

    # ---- socket server --------------------------------------------------------------------------------------

    def handle(self, msg: dict, reply) -> None:
        """Runs one request; `reply(dict)` sends the answer (from another thread for run+wait)."""
        op = msg.get("op")
        if op == "ping":
            reply({"pong": True})
        elif op == "skills":
            reply({"skills": SKILLS, "max_count": MAX_COUNT})
        elif op == "run":
            job = self.submit(str(msg.get("skill", "")), msg.get("count", 1))
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
        elif op == "relax":
            self.stop()
            with self.lock:
                self.body.relax()
                self.torque_maybe_on = False
            reply({"relaxed": True})
        else:
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
