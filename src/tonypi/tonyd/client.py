"""Client for tonyd's Unix socket. Thread-safe; reconnects on its own when tonyd restarts."""

from __future__ import annotations

import itertools
import json
import logging
import socket
import threading
import time
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeout
from typing import Callable

from tonypi.tonyd import SOCK

log = logging.getLogger(__name__)


class TonydError(RuntimeError):
    pass


class TonydClient:
    def __init__(self, path: str = SOCK, on_event: Callable[[dict], None] | None = None) -> None:
        self.path = path
        self.on_event = on_event
        self._sock: socket.socket | None = None
        self._lock = threading.Lock()  # connect + send
        self._pending: dict[int, Future] = {}
        self._rids = itertools.count(1)
        if on_event:  # keep a connection (and the subscription) up from the start
            threading.Thread(target=self._keepalive, name="tonyd-client", daemon=True).start()

    def _connect(self) -> socket.socket:
        if self._sock is not None:
            return self._sock
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            s.connect(self.path)
        except OSError as e:
            s.close()
            raise TonydError(f"tonyd not reachable at {self.path} ({e})") from e
        self._sock = s
        threading.Thread(target=self._reader, args=(s,), daemon=True).start()
        if self.on_event:
            s.sendall(b'{"op": "subscribe"}\n')
        return s

    def _reader(self, s: socket.socket) -> None:
        try:
            for line in s.makefile("r", encoding="utf-8"):
                msg = json.loads(line)
                if "event" in msg:
                    if self.on_event:
                        try:
                            self.on_event(msg)
                        except Exception:
                            log.exception("event handler failed")
                    continue
                fut = self._pending.pop(msg.get("rid"), None)
                if fut is not None and not fut.done():
                    fut.set_result(msg)
        except (OSError, ValueError):
            pass
        with self._lock:
            if self._sock is s:
                self._sock = None
        for rid, fut in list(self._pending.items()):
            if not fut.done():
                fut.set_exception(TonydError("connection to tonyd lost"))
            self._pending.pop(rid, None)

    def _keepalive(self) -> None:
        while True:
            try:
                with self._lock:
                    self._connect()
            except TonydError:
                pass
            time.sleep(2)

    def call(self, op: str, timeout: float = 10.0, **kw) -> dict:
        rid = next(self._rids)
        fut: Future = Future()
        self._pending[rid] = fut
        try:
            with self._lock:
                s = self._connect()
                s.sendall((json.dumps({"op": op, "rid": rid, **kw}) + "\n").encode())
        except OSError as e:
            self._pending.pop(rid, None)
            with self._lock:
                self._sock = None
            raise TonydError(f"tonyd send failed ({e})") from e
        try:
            msg = fut.result(timeout)
        except FutureTimeout as e:
            raise TonydError(f"tonyd did not answer {op} within {timeout} s") from e
        finally:
            self._pending.pop(rid, None)
        if not msg.get("ok"):
            raise TonydError(msg.get("error", "error"))
        msg.pop("ok", None)
        msg.pop("rid", None)
        return msg

    def close(self) -> None:
        with self._lock:
            s, self._sock = self._sock, None
        if s is not None:
            s.close()

    # ---- conveniences -----------------------------------------------------------------------------------------

    def run(self, skill: str, count: int = 1, wait: bool = True, timeout: float = 180, **kw) -> dict:
        """Returns the job dict (state: done / failed / cancelled, plus anything the body said).
        kw: guard_tilt_deg, label, trace, hold_s (see tonyd)."""
        r = self.call("run", timeout=timeout + 5 if wait else 10, skill=skill, count=count, wait=wait,
                      wait_s=timeout, **kw)
        return r["job"]

    def job(self, job_id: int | None = None) -> dict:
        """A finished job's record (tonyd keeps the last 200; all of them in jobs.jsonl). None = the last one."""
        return self.call("job", id=job_id)["job"]

    def stop(self, latch: bool = False) -> None:
        self.call("stop", latch=latch)

    def unlatch(self) -> None:
        self.call("unlatch")

    def state(self, fresh: bool = False) -> dict:
        return self.call("state", fresh=fresh)["state"]

    def head(self, pan_deg: float = 0.0, tilt_deg: float = 0.0, ms: int = 400) -> dict:
        return self.call("head", pan_deg=pan_deg, tilt_deg=tilt_deg, ms=ms)["head_deg"]

    def snapshot(self) -> dict:
        return self.call("snapshot")

    def scan(self, pans=(60, 0, -60), tilt_deg: float | None = None) -> dict:
        return self.call("scan", timeout=30, pans=list(pans), tilt_deg=tilt_deg)

    def burst(self, frames: int = 4, seconds: float = 2.0) -> dict:
        return self.call("burst", timeout=float(seconds) + 15, frames=frames, seconds=seconds)

    def skills(self) -> dict[str, str]:
        return self.call("skills")["skills"]

    # ---- writing your own moves (see tonyd/studio.py) ------------------------------------------------------------

    def joints(self) -> dict:
        return self.call("joints")

    def play(self, frames: list[dict], hold_s: float = 0, wait: bool = True, timeout: float = 180, **kw) -> dict:
        r = self.call("play", timeout=timeout + 5 if wait else 10, frames=frames, hold_s=hold_s, wait=wait,
                      wait_s=timeout, **kw)
        return r["job"]

    def pose(self, positions: dict, ms: int = 500, hold_s: float = 0, wait: bool = True) -> dict:
        return self.call("pose", timeout=30, positions=positions, ms=ms, hold_s=hold_s, wait=wait, wait_s=25)["job"]

    def torque(self, ids: list[int] | None, on: bool, hold_s: float = 0) -> None:
        self.call("torque", ids=ids, on=on, hold_s=hold_s)
