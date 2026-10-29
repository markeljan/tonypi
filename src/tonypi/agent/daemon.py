"""tonypi-agent: the robot's always-on process.

Owns the board, speaker and mic. Commands come from the terminal (Unix socket, see chat.py) or by voice
("hey Tony, <command>"). The newest command preempts the running one; "stop" is handled without the LLM.
Events (heard / say / action / info / done) are broadcast to every connected terminal.
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
from pathlib import Path

from tonypi.agent import vision
from tonypi.agent.body import LOW_BATTERY_MV, Body
from tonypi.agent.brain import STOP_RE, Brain
from tonypi.agent.speech import Listener, Speaker

log = logging.getLogger("tonypi.agent")
SOCK = os.environ.get("TONYPI_SOCK", "/run/tonypi/agent.sock")
SNAP = Path(os.environ.get("TONYPI_SNAP", "/run/tonypi/snapshot.jpg"))
IDLE_HOT_C = 80


class Agent:
    def __init__(self, voice: bool = True) -> None:
        self.body = Body()
        self.speaker = Speaker()
        self.brain = Brain()
        self.jobs: queue.Queue = queue.Queue(maxsize=1)
        self.clients: set[socket.socket] = set()
        self.clients_lock = threading.Lock()
        self.ids = itertools.count(1)
        self.busy = threading.Event()
        self.last_warn = 0.0
        self.listener = Listener(self.speaker, self._on_wake, lambda t: self.submit(t, "voice")) if voice else None

    # ---- events -----------------------------------------------------------------------------------------------

    def emit(self, type_: str, **kw) -> None:
        ev = {"type": type_, "t": round(time.time(), 2), **kw}
        log.info("%s %s", type_, {k: v for k, v in kw.items()})
        line = (json.dumps(ev) + "\n").encode()
        with self.clients_lock:
            for c in list(self.clients):
                try:
                    c.sendall(line)
                except OSError:
                    self.clients.discard(c)

    # ---- commands ---------------------------------------------------------------------------------------------

    def submit(self, text: str, source: str) -> int:
        rid = next(self.ids)
        text = text.strip()
        self.emit("heard", id=rid, source=source, text=text)
        if not text:
            self.emit("info", id=rid, text="didn't catch a command after the wake phrase")
            self.emit("done", id=rid)
            return rid
        # anything new interrupts what's running; "stop" does nothing else
        self.body.cancel()
        self.speaker.stop()
        if STOP_RE.match(text):
            self.emit("action", id=rid, name="stop", count=1)
            self.emit("done", id=rid)
            return rid
        try:
            self.jobs.get_nowait()  # drop a queued, not-yet-started command
        except queue.Empty:
            pass
        self.jobs.put((rid, text))
        return rid

    def _on_wake(self) -> None:
        self.emit("wake")
        threading.Thread(target=self.speaker.chirp, daemon=True).start()

    def _worker(self) -> None:
        while True:
            rid, text = self.jobs.get()
            self.busy.set()
            self.body.cancelled.clear()
            try:
                self._execute(rid, text)
            except Exception as e:
                log.exception("command failed")
                self.emit("info", id=rid, text=f"error: {e}")
            finally:
                self.busy.clear()
                self.emit("done", id=rid)

    def _execute(self, rid: int, text: str) -> None:
        t = time.monotonic()
        self.emit("thinking", id=rid)
        plan = self.brain.plan(text)
        self.emit("plan", id=rid, actions=plan.actions, say=plan.say, source=plan.source,
                  seconds=round(time.monotonic() - t, 1))
        if self.body.cancelled.is_set():
            return

        def say(s: str) -> None:
            if s and not self.body.cancelled.is_set():
                self.emit("say", id=rid, text=s)
                self.speaker.say(s)

        speech = threading.Thread(target=say, args=(plan.say,), daemon=True)
        speech.start()
        moved = False
        for name, count in plan.actions:
            if self.body.cancelled.is_set():
                break
            self.emit("action", id=rid, name=name, count=count)
            if name == "describe_view":
                self._describe(rid, say, speech)
            elif name == "report_status":
                speech.join()
                say(self._status_sentence())
            elif name != "stop":
                ok = self.body.run(name, count, lambda s: (speech.join(), say(s)))
                moved = moved or not name.startswith(("look_", "nod", "shake"))
                if not ok:
                    break
        if moved:
            self.body.settle()
        speech.join()

    def _describe(self, rid: int, say, speech: threading.Thread) -> None:
        try:
            vision.snapshot(SNAP)
        except Exception as e:
            speech.join()
            say("My camera isn't working right now.")
            self.emit("info", id=rid, text=f"camera error: {e}")
            return
        self.emit("info", id=rid, text=f"snapshot saved to {SNAP}; describing (~25 s)")
        desc = vision.describe(SNAP, self.body.cancelled)
        speech.join()
        if desc:
            say(desc)

    def _status_sentence(self) -> str:
        st = self.body.status()
        parts = []
        if st["tilt_deg"] is not None:
            parts.append("I'm lying down." if st["lying"] else f"I'm upright, tilted {st['tilt_deg']:.0f} degrees.")
        if st["battery_mv"]:
            parts.append(f"My battery is at {st['battery_mv'] / 1000:.1f} volts.")
        if st["hottest"]:
            _, joint, temp = st["hottest"]
            parts.append(f"My warmest joint is my {joint}, at {temp} degrees.")
        return " ".join(parts) or "I can't read my sensors right now."

    # ---- background checks ------------------------------------------------------------------------------------

    def _watchdog(self) -> None:
        while True:
            time.sleep(20)
            if self.busy.is_set() or time.monotonic() - self.last_warn < 300:
                continue
            try:
                st = self.body.status()
            except Exception:
                log.exception("status failed")
                continue
            msg = None
            if st["hottest"] and st["hottest"][2] >= IDLE_HOT_C:
                _, joint, temp = st["hottest"]
                msg = (f"My {joint} is at {temp} degrees from holding this pose. "
                       "Please lay me down or switch off my servos for a while.")
            elif st["battery_mv"] and st["battery_mv"] < LOW_BATTERY_MV:
                msg = f"My battery is low, at {st['battery_mv'] / 1000:.1f} volts. Please charge me."
            if msg:
                self.last_warn = time.monotonic()
                self.emit("info", text=msg)
                self.speaker.say(msg)

    # ---- socket server ----------------------------------------------------------------------------------------

    def _serve(self) -> None:
        path = Path(SOCK)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.unlink(missing_ok=True)
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(str(path))
        os.chmod(path, 0o660)
        srv.listen(8)
        log.info("listening on %s", path)
        while True:
            conn, _ = srv.accept()
            threading.Thread(target=self._client, args=(conn,), daemon=True).start()

    def _client(self, conn: socket.socket) -> None:
        with self.clients_lock:
            self.clients.add(conn)
        try:
            for line in conn.makefile("r", encoding="utf-8"):
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                    text = msg.get("text", "") if isinstance(msg, dict) else str(msg)
                except json.JSONDecodeError:
                    text = line
                if text == "/status":
                    st = self.body.status()
                    self.emit("status", **st, busy=self.busy.is_set())
                    continue
                rid = self.submit(text, "terminal")
                conn.sendall((json.dumps({"type": "accepted", "id": rid}) + "\n").encode())
        except OSError:
            pass
        finally:
            with self.clients_lock:
                self.clients.discard(conn)
            conn.close()

    def run(self) -> None:
        threading.Thread(target=self._worker, name="worker", daemon=True).start()
        threading.Thread(target=self._watchdog, name="watchdog", daemon=True).start()
        threading.Thread(target=self._serve, name="socket", daemon=True).start()
        if self.listener:
            self.listener.start()
        self.speaker.say("Tony is ready." + (" Say hey Tony, then your command." if self.listener else ""))
        try:
            threading.Event().wait()
        finally:
            if self.listener:
                self.listener.stop()
            self.body.close()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--no-voice", action="store_true", help="terminal commands only (no mic)")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    Agent(voice=not args.no_voice).run()


if __name__ == "__main__":
    main()
