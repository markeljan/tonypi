"""Client for tonypi-voice (see service.py). Thread-based, like TonydClient; reconnects on its own."""

from __future__ import annotations

import itertools
import json
import socket
import threading
import time
from concurrent.futures import Future
from typing import Callable

from tonypi.voice import SOCK


class VoiceClient:
    def __init__(self, name: str, on_event: Callable[[dict], None], path: str = SOCK) -> None:
        """name: the brain ("own", "hermes", "pi"); on_event gets heard / barge_in / transcript / detached."""
        self.name, self.on_event, self.path = name, on_event, path
        self._sock: socket.socket | None = None
        self._lock = threading.Lock()
        self._pending: dict[int, Future] = {}
        self._rids = itertools.count(1)
        threading.Thread(target=self._keep, name="voice-client", daemon=True).start()

    def _keep(self) -> None:
        while True:
            if self._sock is None:
                try:
                    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    s.connect(self.path)
                    self._sock = s
                    threading.Thread(target=self._read, args=(s,), daemon=True).start()
                    self.call("attach", name=self.name)
                except OSError:
                    self._sock = None
            time.sleep(2)

    def _read(self, s: socket.socket) -> None:
        try:
            for line in s.makefile("r", encoding="utf-8"):
                msg = json.loads(line)
                if "event" in msg:
                    self.on_event(msg)
                elif (f := self._pending.pop(msg.get("rid"), None)) is not None:
                    f.set_result(msg)
        except (OSError, ValueError):
            pass
        self._sock = None

    def call(self, op: str, timeout: float = 10, **kw) -> dict:
        s = self._sock
        if s is None:
            raise ConnectionError("tonypi-voice not reachable")
        rid = next(self._rids)
        fut: Future = Future()
        self._pending[rid] = fut
        with self._lock:
            s.sendall((json.dumps({"op": op, "rid": rid, **kw}) + "\n").encode())
        msg = fut.result(timeout)
        if not msg.get("ok"):
            raise RuntimeError(msg.get("error"))
        return msg

    def reply(self, rid: int, text: str, final: bool = True) -> None:
        self.call("reply", id=rid, text=text, final=final)

    def say(self, text: str) -> None:
        self.call("say", text=text)

    def ask(self, text: str, timeout_s: float = 30) -> str | None:
        return self.call("ask", timeout=timeout_s + 10, text=text, timeout_s=timeout_s)["answer"]
