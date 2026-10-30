"""Client of the shared `tonypi-voice` service (GPT-Live-1 voice, docs/GPT_LIVE.md): it owns the USB mic and speaker
for every harness; we attach as the active brain and get the user's requests as text.

  -> {"op":"attach","name":"own"}            <- {"event":"heard","id":N,"text":...}  /  {"event":"barge_in"}
  -> {"op":"reply","id":N,"text":...,"final":bool}   -> {"op":"say","text":...}
  -> {"op":"ask","text":...,"timeout_s":30,"rid":R}  <- {"rid":R,"answer":... | null}
  -> {"op":"status"}

Requests (`heard`) become harness commands (steering if a task runs; "stop" never reaches a model). Progress lines
go back as non-final replies to the newest open request; the task's last word is the final reply; any other request
of the same task is closed with an empty final reply. If the service isn't there, the harness uses its local voice.
"""

from __future__ import annotations

import itertools
import json
import logging
import os
import socket
import threading
import time
from concurrent.futures import Future
from typing import Callable

log = logging.getLogger(__name__)

SOCK = os.environ.get("TONYPI_VOICE_SOCK", "/run/tonypi/voice.sock")
NAME = os.environ.get("TONYPI_VOICE_NAME", "own")
MAX_CHARS = 400


class VoiceLink:
    def __init__(self, on_heard: Callable[[int, str], None], on_barge_in: Callable[[], None] | None = None,
                 path: str = SOCK, name: str = NAME, attach: bool = True) -> None:
        self.path, self.name, self.attach = path, name, attach  # attach=False: status only (don't take the voice)
        self.on_heard, self.on_barge_in = on_heard, on_barge_in
        self.sock: socket.socket | None = None
        self.wlock = threading.Lock()
        self.connected = threading.Event()
        self.open_ids: list[int] = []  # requests of the running task still waiting for their final reply
        self.pending: dict[int, Future] = {}
        self.rids = itertools.count(1)
        self.speaking_until = 0.0
        threading.Thread(target=self._loop, name="voicelink", daemon=True).start()

    @staticmethod
    def available(path: str = SOCK) -> bool:
        """The service is up (a stale socket file doesn't count)."""
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(1)
        try:
            s.connect(path)
            return True
        except OSError:
            return False
        finally:
            s.close()

    # ---- connection ---------------------------------------------------------------------------------------------

    def _loop(self) -> None:
        while True:
            try:
                s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                s.connect(self.path)
                self.sock = s
                if self.attach:
                    self._send({"op": "attach", "name": self.name})
                self.connected.set()
                log.info("voice service attached as %r", self.name)
                for line in s.makefile("r", encoding="utf-8"):
                    self._handle(json.loads(line))
            except (OSError, ValueError) as e:
                log.debug("voice service: %s", e)
            self.connected.clear()
            self.sock = None
            for fut in list(self.pending.values()):
                if not fut.done():
                    fut.set_result({"answer": None})
            time.sleep(2)

    def _send(self, msg: dict) -> bool:
        with self.wlock:
            if self.sock is None:
                return False
            try:
                self.sock.sendall((json.dumps(msg) + "\n").encode())
                return True
            except OSError:
                return False

    def _handle(self, msg: dict) -> None:
        ev = msg.get("event")
        if ev == "heard":
            self.on_heard(int(msg["id"]), str(msg.get("text", "")))
        elif ev == "barge_in":
            if self.on_barge_in:
                self.on_barge_in()
        elif ev == "detached":
            log.warning("voice service: another brain attached (%s)", msg)
        elif (fut := self.pending.pop(msg.get("rid"), None)) is not None and not fut.done():
            fut.set_result(msg)

    # ---- speaking -----------------------------------------------------------------------------------------------

    def task_started(self, voice_id: int) -> None:
        self.open_ids.append(voice_id)

    def speak(self, text: str, final: bool = False) -> None:
        text = text.strip()[:MAX_CHARS]
        if not text:
            return
        if self.open_ids:
            vid = self.open_ids[-1]
            self._send({"op": "reply", "id": vid, "text": text, "final": final})
            if final:
                self.open_ids.remove(vid)
        else:
            self._send({"op": "say", "text": text})

    def close(self, voice_id: int) -> None:
        """A request that started no task ('stop', steering words already answered): an empty final reply."""
        self._send({"op": "reply", "id": voice_id, "text": "", "final": True})

    def task_ended(self) -> None:
        """Close every request of the finished task that hasn't had its final reply."""
        for vid in self.open_ids:
            self._send({"op": "reply", "id": vid, "text": "", "final": True})
        self.open_ids = []

    def ask(self, question: str, timeout_s: float = 30) -> str | None:
        """Ask the user out loud; their spoken answer, or None (no answer in time / no voice service)."""
        rid = next(self.rids)
        fut: Future = Future()
        self.pending[rid] = fut
        if not self._send({"op": "ask", "rid": rid, "text": question[:MAX_CHARS], "timeout_s": timeout_s}):
            self.pending.pop(rid, None)
            return None
        try:
            return (fut.result(timeout_s + 10) or {}).get("answer")
        except Exception:
            return None
        finally:
            self.pending.pop(rid, None)

    def status(self) -> dict:
        rid = next(self.rids)
        fut: Future = Future()
        self.pending[rid] = fut
        if not self._send({"op": "status", "rid": rid}):
            return {"connected": False}
        try:
            return {"connected": True, **fut.result(5)}
        except Exception:
            return {"connected": True, "status": "no answer"}
        finally:
            self.pending.pop(rid, None)
