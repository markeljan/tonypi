"""tonypi-pi: the robot's front end with pi as the brain (harness bake-off option 3).

One long-lived pi session (`pi --mode rpc`, our extension in pi/extension gives it the robot's tools) gets every
command, typed (Unix socket, tonypi-chat protocol) or spoken ("hey Tony, ..."). This process owns the mic and
speaker; tonyd owns the body. Reflexes never wait for the model:

- "stop" (and friends) goes straight to tonyd, kills speech and aborts pi;
- a new command while pi works steers it: if a tool is running (a walk), the message is queued as a pi steer and
  the motion tools return at the next step boundary, so the model reads it after <= ~1.75 s; if the model is
  generating, the run is aborted and the new command prompted at once;
- tonyd's warnings (overheating, low battery) are spoken as they come.
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

from tonypi.agent.brain import STOP_RE
from tonypi.pi_front.rpc import PiRpc
from tonypi.tonyd.client import TonydClient, TonydError

log = logging.getLogger("tonypi.pi_front")
SOCK = os.environ.get("TONYPI_PI_SOCK", "/run/tonypi/pi.sock")
LOG = Path(os.environ.get("TONYPI_PI_LOG", "/home/pi/.local/state/tonypi/pi-front.jsonl"))
IDLE_RESET_S = float(os.environ.get("TONYPI_PI_IDLE_RESET_S", "600"))  # fresh pi session after this long idle
MOTION_TOOLS = {"run_skill", "look"}


class Speech:
    """Ordered, interruptible speech in its own thread (Speaker.say blocks)."""

    def __init__(self, speaker) -> None:
        self.speaker = speaker
        self.q: queue.Queue[str] = queue.Queue()
        self.idle = threading.Event()
        self.idle.set()
        threading.Thread(target=self._loop, name="speech", daemon=True).start()

    def say(self, text: str) -> None:
        if text.strip():
            self.idle.clear()
            self.q.put(text)

    def stop(self) -> None:
        while not self.q.empty():
            try:
                self.q.get_nowait()
            except queue.Empty:
                break
        if self.speaker:
            self.speaker.stop()

    def _loop(self) -> None:
        while True:
            text = self.q.get()
            try:
                if self.speaker:
                    self.speaker.say(text)
            except Exception:
                log.exception("speech failed")
            if self.q.empty():
                self.idle.set()


class Command:
    def __init__(self, rid: int, text: str, source: str) -> None:
        self.rid, self.text, self.source = rid, text, source
        self.t0 = time.monotonic()
        self.marks: dict[str, float] = {}
        self.tools: list[str] = []
        self.models: set[str] = set()
        self.tokens = {"input": 0, "output": 0, "cacheRead": 0}
        self.cost = 0.0
        self.how = "prompt"

    def mark(self, name: str) -> None:
        self.marks.setdefault(name, round(time.monotonic() - self.t0, 2))


class Front:
    def __init__(self, voice: bool = True, speak: bool = True, model: str = "tony/brain") -> None:
        speaker = None
        if speak or voice:
            from tonypi.agent.speech import Speaker
            speaker = Speaker()
        self.speech = Speech(speaker)
        self.tonyd = TonydClient(on_event=self._on_tonyd)
        self.pi = PiRpc(self._on_pi, model=model)
        self.ids = itertools.count(1)
        self.cmd: Command | None = None  # the newest command; pi's current run belongs to it
        self.running = False  # pi has a run in progress (agent_start .. agent_settled)
        self.tools_running = 0
        self.last_settled = time.monotonic()
        self.restarting = False
        self.lock = threading.RLock()
        self.clients: set[socket.socket] = set()
        self.clients_lock = threading.Lock()
        self.listener = None
        if voice:
            from tonypi.agent.speech import Listener
            self.listener = Listener(speaker, lambda: self.emit("wake"),
                                     lambda t, a=None, recording=None: self.submit(t, "voice"))

    # ---- events to terminals --------------------------------------------------------------------------------

    def emit(self, type_: str, **kw) -> None:
        ev = {"type": type_, "t": round(time.time(), 2), **kw}
        log.info("%s %s", type_, kw)
        line = (json.dumps(ev) + "\n").encode()
        with self.clients_lock:
            for c in list(self.clients):
                try:
                    c.sendall(line)
                except OSError:
                    self.clients.discard(c)

    # ---- commands -------------------------------------------------------------------------------------------

    def submit(self, text: str, source: str) -> int:
        rid = next(self.ids)
        text = text.strip()
        self.emit("heard", id=rid, source=source, text=text)
        if not text:
            self.emit("done", id=rid)
            return rid
        if STOP_RE.match(text):
            self.stop_all()
            self.emit("action", id=rid, name="stop", count=1)
            self.emit("done", id=rid)
            return rid
        self.speech.stop()  # barge-in
        with self.lock:
            old, self.cmd = self.cmd, Command(rid, text, source)
            if self.running and old is not None:
                if self.tools_running:
                    self.cmd.how = "steer"  # the motion tool returns at its next step; the model reads this next
                    self.pi.send_async("steer", message=text)
                else:
                    self.cmd.how = "abort+prompt"  # mid-generation: don't wait for a reply nobody wants now
                    threading.Thread(target=self._restart, args=(text,), daemon=True).start()
                self._finish(old, superseded=True)
            else:
                if time.monotonic() - self.last_settled > IDLE_RESET_S:
                    self.pi.send_async("new_session")
                self.running = True  # until agent_settled (set before pi answers, so a quick 2nd command steers)
                self.pi.send_async("prompt", message=text, streamingBehavior="steer")
        self.emit("info", id=rid, text=f"→ pi ({self.cmd.how})")
        return rid

    def _restart(self, text: str) -> None:
        self.restarting = True  # the aborted run's agent_settled must not finish the new command
        try:
            self.pi.send("clear_queue")
            self.pi.send("abort", timeout=60)
            self.running = True
            self.pi.send("prompt", message=text, streamingBehavior="steer")
        except Exception as e:
            log.warning("restart failed: %s", e)
        finally:
            self.restarting = False

    def stop_all(self) -> None:
        """No LLM involved: body first, then speech, then pi's run."""
        try:
            self.tonyd.stop()
        except TonydError as e:
            log.warning("tonyd stop failed: %s", e)
        self.speech.stop()
        with self.lock:
            if self.running:
                self.pi.send_async("clear_queue")
                self.pi.send_async("abort")
            if self.cmd:
                self.cmd.how += "+stopped"

    # ---- pi events ------------------------------------------------------------------------------------------

    def _on_pi(self, ev: dict) -> None:
        t = ev.get("type")
        cmd = self.cmd
        if t == "agent_start":
            self.running = True
        elif t == "tool_execution_start":
            self.tools_running += 1
            name, args = ev.get("toolName"), ev.get("args") or {}
            if cmd:
                cmd.mark("first_tool")
                cmd.tools.append(name if name != "run_skill" else f"{args.get('skill')}x{args.get('count', 1)}")
                if name in MOTION_TOOLS:
                    cmd.mark("first_motion")
            if name == "say":
                self._say(args.get("text", ""), cmd)
            else:
                self.emit("action", id=cmd and cmd.rid, name=name if name != "run_skill" else args.get("skill"),
                          count=args.get("count", 1), args=args)
        elif t == "tool_execution_end":
            self.tools_running = max(0, self.tools_running - 1)
            res = ev.get("result") or {}
            texts = [c.get("text", "") for c in res.get("content", []) if c.get("type") == "text"]
            if ev.get("toolName") != "say":
                self.emit("info", id=cmd and cmd.rid, text=f"{ev.get('toolName')}: {' '.join(texts)[:200]}"
                          + (" [error]" if ev.get("isError") else ""))
        elif t == "message_end" and (ev.get("message") or {}).get("role") == "assistant":
            m = ev["message"]
            if cmd:
                cmd.models.add(f"{m.get('provider')}/{m.get('model')}")
                u = m.get("usage") or {}
                for k in cmd.tokens:
                    cmd.tokens[k] += u.get(k, 0)
                cmd.cost += (u.get("cost") or {}).get("total", 0)
            text = " ".join(c.get("text", "") for c in m.get("content", []) if c.get("type") == "text").strip()
            if m.get("stopReason") == "error":
                self.emit("info", id=cmd and cmd.rid, text=f"model error: {m.get('errorMessage')}")
            elif text and m.get("stopReason") != "aborted":
                self._say(text, cmd)
        elif t in ("auto_retry_start", "auto_retry_end", "compaction_start", "compaction_end"):
            self.emit("info", id=cmd and cmd.rid, text=f"pi {t} {ev.get('errorMessage') or ''}".strip())
        elif t == "agent_settled" and self.restarting:
            self.tools_running = 0
        elif t == "agent_settled":
            with self.lock:
                self.running = False
                self.tools_running = 0
                self.last_settled = time.monotonic()
                if cmd is self.cmd and cmd is not None:
                    self.cmd = None
                    self._finish(cmd, error=bool(ev.get("aborted")))
        elif t == "pi_exit":
            self.emit("info", text=f"pi exited ({ev.get('code')}); restarting")
            self.running, self.tools_running = False, 0
            threading.Thread(target=self._respawn, daemon=True).start()

    def _respawn(self) -> None:
        time.sleep(2)
        self.pi.start()

    def _say(self, text: str, cmd: Command | None) -> None:
        if cmd:
            cmd.mark("first_speech")
        self.emit("say", id=cmd and cmd.rid, text=text)
        self.speech.say(text)

    def _finish(self, cmd: Command, superseded: bool = False, error: bool = False) -> None:
        cmd.mark("done")
        rec = {"id": cmd.rid, "text": cmd.text, "source": cmd.source, "how": cmd.how, "tools": cmd.tools,
               "models": sorted(cmd.models), "tokens": cmd.tokens, "cost_usd": round(cmd.cost, 5), **cmd.marks,
               "superseded": superseded, "aborted": error, "wall": time.strftime("%H:%M:%S")}
        try:
            LOG.parent.mkdir(parents=True, exist_ok=True)
            with LOG.open("a") as f:
                f.write(json.dumps(rec) + "\n")
        except OSError:
            pass
        self.emit("timing", id=cmd.rid, **{k: v for k, v in rec.items() if k not in ("id", "text")})
        self.emit("done", id=cmd.rid)
        if cmd.source == "voice" and self.listener and not superseded:
            def followup() -> None:
                self.speech.idle.wait(60)
                self.listener.followup()
            threading.Thread(target=followup, daemon=True).start()

    # ---- tonyd ----------------------------------------------------------------------------------------------

    def _on_tonyd(self, ev: dict) -> None:
        if ev.get("event") == "job" and ev.get("state") in ("done", "failed", "cancelled"):
            self.emit("body", job=ev.get("id"), skill=ev.get("skill"), state=ev.get("state"))
        if ev.get("event") == "warn":  # overheating / low battery: no model needed
            self.emit("info", text=ev.get("text", ""))
            if not self.running:
                self.speech.say(ev.get("text", ""))

    # ---- socket (tonypi-chat protocol) ---------------------------------------------------------------------

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
                except json.JSONDecodeError:
                    msg = {"text": line}
                if not isinstance(msg, dict):
                    msg = {"text": str(msg)}
                if msg.get("op") == "say":
                    self._say(msg.get("text", ""), None)
                    conn.sendall(b'{"type": "accepted", "op": "say"}\n')
                    continue
                if msg.get("text") == "/status":
                    st = self.tonyd.state(fresh=True)
                    self.emit("status", **st)
                    continue
                rid = self.submit(msg.get("text", ""), msg.get("source", "terminal"))
                conn.sendall((json.dumps({"type": "accepted", "id": rid}) + "\n").encode())
        except OSError:
            pass
        finally:
            with self.clients_lock:
                self.clients.discard(conn)
            conn.close()

    def run(self) -> None:
        self.pi.start()
        threading.Thread(target=self._serve, name="socket", daemon=True).start()
        if self.listener:
            self.listener.start()
        self.speech.say("Tony is ready.")
        try:
            threading.Event().wait()
        finally:
            if self.listener:
                self.listener.stop()
            self.pi.close()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--no-voice", action="store_true", help="no mic (typed commands only)")
    ap.add_argument("--mute", action="store_true", help="no speaker either (text benchmarks)")
    ap.add_argument("--model", default=os.environ.get("TONYPI_PI_MODEL", "tony/brain"))
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    Front(voice=not args.no_voice, speak=not args.mute, model=args.model).run()


def chat() -> None:
    """tonypi-pi-chat: tonypi-chat pointed at this front end's socket."""
    os.environ.setdefault("TONYPI_SOCK", SOCK)
    from tonypi.agent import chat as c
    c.SOCK = os.environ["TONYPI_SOCK"]
    c.main()


if __name__ == "__main__":
    main()
