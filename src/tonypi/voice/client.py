"""Client for tonypi-voice (see service.py): one short connection per request, no attach or events."""

from __future__ import annotations

import itertools
import json
import socket

from tonypi.voice import SOCK

_rids = itertools.count(1)


class VoiceClient:
    def __init__(self, path: str = SOCK, timeout: float = 10) -> None:
        self.path, self.timeout = path, timeout

    def call(self, op: str, timeout: float | None = None, **kw) -> dict:
        rid = next(_rids)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(timeout or self.timeout)
            try:
                s.connect(self.path)
            except OSError as e:
                raise ConnectionError(f"tonypi-voice not reachable at {self.path} ({e})") from e
            s.sendall((json.dumps({"op": op, "rid": rid, **kw}) + "\n").encode())
            for line in s.makefile("r", encoding="utf-8"):
                msg = json.loads(line)
                if msg.get("rid") == rid:
                    break
            else:
                raise ConnectionError("tonypi-voice closed the connection")
        if not msg.get("ok"):
            raise RuntimeError(msg.get("error"))
        return msg

    def say(self, text: str) -> None:
        self.call("say", text=text)

    def text(self, text: str, source: str = "typed") -> dict:
        """A typed request for the live brain (a turn, or a steer of the running one)."""
        return self.call("text", timeout=15, text=text, source=source)

    def status(self) -> dict:
        return self.call("status")

    def tasks(self) -> dict:
        return self.call("tasks")["tasks"]
