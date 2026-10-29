"""Peripherals: the ways people reach Tony. Each one turns input into `front.submit(text, source)` and may show or
speak `front` events. Add a new one by subclassing Peripheral and listing it in `enabled()`.

- SocketPeripheral: /run/tonypi/agent.sock, the same JSON-lines protocol as tonypi-agent, so `tonypi-chat` works
  unchanged and the MCP server's `say` tool reaches the speaker.
- VoicePeripheral: "hey Tony ..." through tonypi.agent.speech (keyword spotter, streaming ASR, LFM3 re-transcription).
- IPhonePeripheral: TODO.
Telegram / Hermes Desktop need nothing here: they talk to the Hermes gateway directly (same profile, same tools).
"""

from __future__ import annotations

import json
import logging
import os
import socket
import threading
from pathlib import Path

from tonypi.hermes.front import Front

log = logging.getLogger("tonypi.hermes")
SOCK = os.environ.get("TONYPI_SOCK", "/run/tonypi/agent.sock")


class Peripheral:
    name = "peripheral"

    def __init__(self, front: Front) -> None:
        self.front = front

    def start(self) -> None:
        raise NotImplementedError


class SocketPeripheral(Peripheral):
    name = "terminal"

    def __init__(self, front: Front, path: str = SOCK) -> None:
        super().__init__(front)
        self.path = path
        self.clients: set[socket.socket] = set()
        self.lock = threading.Lock()
        front.listeners.append(self._broadcast)

    def _broadcast(self, ev: dict) -> None:
        line = (json.dumps(ev) + "\n").encode()
        with self.lock:
            for c in list(self.clients):
                try:
                    c.sendall(line)
                except OSError:
                    self.clients.discard(c)

    def start(self) -> None:
        path = Path(self.path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.unlink(missing_ok=True)
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(str(path))
        os.chmod(path, 0o660)
        srv.listen(8)
        log.info("listening on %s", path)

        def accept() -> None:
            while True:
                conn, _ = srv.accept()
                threading.Thread(target=self._client, args=(conn,), daemon=True).start()

        threading.Thread(target=accept, name="socket", daemon=True).start()

    def _client(self, conn: socket.socket) -> None:
        with self.lock:
            self.clients.add(conn)
        try:
            for line in conn.makefile("r", encoding="utf-8"):
                if not (line := line.strip()):
                    continue
                try:
                    msg = json.loads(line)
                    text = msg.get("text", "") if isinstance(msg, dict) else str(msg)
                except json.JSONDecodeError:
                    msg, text = None, line
                if isinstance(msg, dict) and msg.get("op") == "say":  # the MCP server's say tool
                    self.front.say(text)
                    conn.sendall((json.dumps({"type": "accepted", "op": "say"}) + "\n").encode())
                elif text == "/status":
                    st = self.front.status()
                    self.front.emit("status", **st, busy=self.front.busy)
                else:
                    rid = self.front.submit(text, "terminal")
                    conn.sendall((json.dumps({"type": "accepted", "id": rid}) + "\n").encode())
        except OSError:
            pass
        finally:
            with self.lock:
                self.clients.discard(conn)
            conn.close()


class VoicePeripheral(Peripheral):
    name = "voice"

    def __init__(self, front: Front, speaker) -> None:
        from tonypi.agent.speech import Listener

        super().__init__(front)
        self.listener = Listener(speaker, lambda: front.emit("wake"),
                                 lambda text, audio=None, recording=None: front.submit(text, "voice"))
        # spoken command answered: the next words need no "hey Tony" (waits for the reply to finish playing)
        front.on_answered.append(self._answered)

    def _answered(self, source: str) -> None:
        if source == "voice":
            def later() -> None:
                threading.Event().wait(0.3)  # the reply is queued just before this; let playback start
                while not self.front.speech.empty() or self.listener.speaker.speaking.is_set():
                    threading.Event().wait(0.1)
                self.listener.followup()
            threading.Thread(target=later, daemon=True).start()

    def start(self) -> None:
        self.listener.start()


class IPhonePeripheral(Peripheral):
    """TODO(iphone): the iPhone app as a peripheral: push-to-talk / text from the phone, Tony's replies and camera
    frames back, maybe the phone's camera and IMU as extra sensors. Likely shape: a small HTTPS/WebSocket endpoint
    here (LAN or Tailscale, token auth) mapping messages onto front.submit(text, "iphone") and streaming front events
    back; audio would go through the same STT as voice. Alternatively the app talks to the Hermes API server directly
    and only uses this for stop + events."""

    name = "iphone"

    def start(self) -> None:
        raise NotImplementedError("iPhone peripheral not built yet")


def make_speaker():
    from tonypi.agent.speech import Speaker

    return Speaker()


def enabled(front: Front, speaker) -> list[Peripheral]:
    out: list[Peripheral] = [SocketPeripheral(front)]
    if speaker is not None:
        out.append(VoicePeripheral(front, speaker))
    return out
