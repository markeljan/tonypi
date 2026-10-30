"""Client for the shared voice peripheral `tonypi-voice` (docs/GPT_LIVE.md): GPT-Live-1 over the ChatGPT subscription,
or the local wake word + ASR + Piper when GPT-Live is down. It owns the USB mic and speaker; a brain attaches and gets
the user's requests as text, and sends back what to say. It never moves the robot.

Unix socket /run/tonypi/voice.sock, JSON lines:
  -> {"op": "attach", "name": "pi"}                  (last attach wins; the others get {"event": "detached"})
  <- {"event": "heard", "id": N, "text": "..."}       a request
  -> {"op": "reply", "id": N, "text": "...", "final": true|false}
  -> {"op": "say", "text": "..."}
  -> {"op": "ask", "text": "...", "timeout_s": 30}  <- {"answer": "..." | null}
  <- {"event": "barge_in"}
  -> {"op": "status"}
"""

from __future__ import annotations

import json
import logging
import os
import socket
import threading
import time
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeout
from typing import Callable

log = logging.getLogger(__name__)
VOICE_SOCK = os.environ.get("TONYPI_VOICE_SOCK", "/run/tonypi/voice.sock")
MAX_CHARS = 400  # per reply, per the service's contract


def available(path: str = VOICE_SOCK) -> bool:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(1)
    try:
        s.connect(path)
        return True
    except OSError:
        return False
    finally:
        s.close()


class VoiceService:
    def __init__(self, on_heard: Callable[[int, str], None], on_barge_in: Callable[[], None] = lambda: None,
                 path: str = VOICE_SOCK, name: str = "pi") -> None:
        self.on_heard, self.on_barge_in = on_heard, on_barge_in
        self.path, self.name = path, name
        self.sock: socket.socket | None = None
        self.wlock = threading.Lock()
        self.attached = False
        self.detached_by_other = False
        self.asking: Future | None = None
        self.status_reply: Future | None = None

    def start(self) -> "VoiceService":
        threading.Thread(target=self._loop, name="voice-service", daemon=True).start()
        return self

    def _loop(self) -> None:
        """Keep a connection and the attachment; after another brain attached, stay out of its way."""
        while True:
            try:
                s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                s.connect(self.path)
                self.sock = s
                self._send({"op": "attach", "name": self.name})
                self.attached, self.detached_by_other = True, False
                log.info("attached to the voice service at %s", self.path)
                for line in s.makefile("r", encoding="utf-8"):
                    self._handle(json.loads(line))
            except (OSError, ValueError) as e:
                log.warning("voice service: %s", e)
            self.attached, self.sock = False, None
            if self.asking and not self.asking.done():
                self.asking.set_result(None)
            if self.detached_by_other:
                return  # someone else is the active brain now: no fight over the mic
            time.sleep(3)

    def _handle(self, msg: dict) -> None:
        ev = msg.get("event")
        if ev == "heard":
            self.on_heard(int(msg.get("id", 0)), str(msg.get("text", "")))
        elif ev == "barge_in":
            self.on_barge_in()
        elif ev == "detached":
            log.warning("voice service: another brain attached (%s)", msg)
            self.attached, self.detached_by_other = False, True
            if self.sock:
                self.sock.close()
        elif "answer" in msg and self.asking and not self.asking.done():
            self.asking.set_result(msg.get("answer"))
        elif self.status_reply and not self.status_reply.done() and ev is None:
            self.status_reply.set_result(msg)

    def _send(self, obj: dict) -> bool:
        s = self.sock
        if s is None:
            return False
        try:
            with self.wlock:
                s.sendall((json.dumps(obj) + "\n").encode())
            return True
        except OSError:
            return False

    def reply(self, id_: int, text: str, final: bool = True) -> bool:
        return self._send({"op": "reply", "id": id_, "text": text[:MAX_CHARS], "final": final})

    def say(self, text: str) -> bool:
        return self._send({"op": "say", "text": text[:MAX_CHARS]})

    def ask(self, text: str, timeout_s: float = 30) -> str | None:
        self.asking = Future()
        if not self._send({"op": "ask", "text": text[:MAX_CHARS], "timeout_s": timeout_s}):
            return None
        try:
            return self.asking.result(timeout_s + 5)
        except FutureTimeout:
            return None
        finally:
            self.asking = None

    def status(self, timeout: float = 3) -> dict | None:
        self.status_reply = Future()
        if not self._send({"op": "status"}):
            return None
        try:
            return self.status_reply.result(timeout)
        except FutureTimeout:
            return None
