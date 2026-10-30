"""Peripherals: the ways people reach Tony. Each one turns input into `front.submit(text, source)` and may show or
speak `front` events. Add a new one by subclassing Peripheral and listing it in `enabled()`.

- SocketPeripheral: /run/tonypi/agent.sock, the same JSON-lines protocol as tonypi-agent, so `tonypi-chat` works
  unchanged and the MCP server's `say` tool reaches the speaker.
- VoiceServicePeripheral: the shared tonypi-voice service (/run/tonypi/voice.sock; GPT-Live-1 over the ChatGPT login,
  with its own wake-word fallback; docs/GPT_LIVE.md). It owns the mic and speaker; we attach as the active brain, get
  "heard" requests, send replies / progress / questions back. Used whenever the service is running.
- VoicePeripheral: local "hey Tony ..." through tonypi.agent.speech (only when tonypi-voice isn't running).
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
VOICE_SOCK = os.environ.get("TONYPI_VOICE_SOCK", "/run/tonypi/voice.sock")
VOICE_MAX_CHARS = 400  # per reply (GPT-Live appends are chunked at 500 bytes)


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
                elif isinstance(msg, dict) and msg.get("op") == "ask":  # tonypi-self's ask_user tool
                    answer = self.front.ask(text, float(msg.get("timeout_s") or 30))
                    conn.sendall((json.dumps({"type": "answer", "answer": answer}) + "\n").encode())
                elif text == "/status":
                    st = self.front.status()
                    self.front.emit("status", **{**st, "busy": self.front.busy, "harness": "hermes"})
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


def _speakable(text: str) -> str:
    """At most VOICE_MAX_CHARS, cut at a sentence end when possible."""
    text = " ".join(text.split())
    if len(text) <= VOICE_MAX_CHARS:
        return text
    cut = text[:VOICE_MAX_CHARS]
    end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
    return cut[:end + 1] if end > VOICE_MAX_CHARS // 2 else cut.rsplit(" ", 1)[0] + "…"


class VoiceServicePeripheral(Peripheral):
    """Client of tonypi-voice. The service turns speech into "heard" requests (GPT-Live delegations or its wake-word
    fallback) and speaks what we send: one final reply per request, progress notes, unprompted lines, questions.
    "stop" arrives as an ordinary request and goes to tonyd without the LLM (Front.submit)."""

    name = "voice-service"

    def __init__(self, front: Front, path: str = VOICE_SOCK) -> None:
        super().__init__(front)
        self.path = path
        self.sock: socket.socket | None = None
        self.send_lock = threading.Lock()
        self.attached = False
        self.voice_ids: dict[int, int] = {}  # front request id → voice service request id
        self.answers: list = []  # the pending ask's reply lands here
        self.answer_ready = threading.Event()
        front.listeners.append(self._on_front_event)

    def _send(self, msg: dict) -> bool:
        with self.send_lock:
            if self.sock is None:
                return False
            try:
                self.sock.sendall((json.dumps(msg) + "\n").encode())
                return True
            except OSError:
                return False

    def start(self) -> None:
        threading.Thread(target=self._loop, name="voice-service", daemon=True).start()

    def _loop(self) -> None:
        while True:
            try:
                s = socket.socket(socket.AF_UNIX)
                s.connect(self.path)
                with self.send_lock:
                    self.sock = s
                self._send({"op": "attach", "name": "hermes"})
                self.attached = True
                log.info("attached to tonypi-voice at %s", self.path)
                for line in s.makefile("r", encoding="utf-8"):
                    try:
                        self._on_message(json.loads(line))
                    except (ValueError, TypeError):
                        log.warning("voice service: bad line %r", line[:200])
            except OSError as e:
                log.debug("voice service: %s", e)
            with self.send_lock:
                self.sock = None
            if self.attached:
                log.warning("tonypi-voice connection lost; retrying")
            self.attached = False
            threading.Event().wait(3)

    def _on_message(self, msg: dict) -> None:
        ev = msg.get("event")
        if ev == "heard":
            text = str(msg.get("text") or "")
            rid = self.front.submit(text, "voice")
            if msg.get("id") is not None:
                self.voice_ids[rid] = msg["id"]
        elif ev == "barge_in":
            self.front.emit("info", text="(barge-in: the user is talking over Tony)")
        elif ev == "detached":  # another brain attached (last attach wins): don't fight over it
            self.attached = False
            log.warning("tonypi-voice: another brain attached; we're detached")
        elif "answer" in msg:
            self.answers.append(msg.get("answer"))
            self.answer_ready.set()

    def _on_front_event(self, ev: dict) -> None:
        if not self.attached:
            return
        kind = ev.get("type")
        if kind == "say":
            text = _speakable(ev.get("text") or "")
            vid = self.voice_ids.get(ev.get("id"))
            if vid is not None:
                self._send({"op": "reply", "id": vid, "text": text, "final": bool(ev.get("final"))})
                if ev.get("final"):
                    self.voice_ids.pop(ev.get("id"), None)
            else:
                self._send({"op": "say", "text": text})
        elif kind == "ask":
            threading.Thread(target=self._ask, args=(ev.get("text") or "", float(ev.get("timeout_s") or 30)),
                             daemon=True).start()

    def _ask(self, question: str, timeout_s: float) -> None:
        self.answers.clear()
        self.answer_ready.clear()
        if not self._send({"op": "ask", "text": _speakable(question), "timeout_s": timeout_s}):
            return
        if self.answer_ready.wait(timeout_s + 5) and self.answers and self.answers[0]:
            self.front.answer(str(self.answers[0]))


def voice_service_up(path: str = VOICE_SOCK) -> bool:
    try:
        s = socket.socket(socket.AF_UNIX)
        s.settimeout(2)
        s.connect(path)
        s.close()
        return True
    except OSError:
        return False


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


def enabled(front: Front, speaker, voice_service: bool = False) -> list[Peripheral]:
    out: list[Peripheral] = [SocketPeripheral(front)]
    if voice_service:
        out.append(VoiceServicePeripheral(front))
    elif speaker is not None:
        out.append(VoicePeripheral(front, speaker))
    return out
