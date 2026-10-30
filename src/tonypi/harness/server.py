"""tonypi-harness: the always-on brain harness (bake-off option 1: our own).

Front ends: a JSON-lines Unix socket (/run/tonypi/harness.sock; `tonypi-harness-chat` or any script) and, with
--voice, the "hey Tony" listener from tonypi.agent.speech. The mic and speaker have one owner: stop tonypi-agent
before running this with --voice or with sound.

Socket protocol (same events as tonypi-agent, so tonypi-chat works with TONYPI_SOCK pointing here):
  -> {"text": "wave"}                 a command (or steering, while a task runs; "stop" never reaches an LLM)
  -> {"op": "say", "text": "hi"}      just speak
  -> "/status"                        a status event
  <- {"type": "heard"|"thinking"|"action"|"tool_result"|"say"|"info"|"plan"|"usage"|"done"|"status", "t": ...}

Environment: TONYPI_BRAIN (default codex,local; claude optional), TONYPI_PERIPHERALS (body,camera,voice,learning,iphone),
TONYPI_CODEX_TIER (auto|fast|multi|advanced), TONYPI_CODEX_FAST/MULTI/ADVANCED ("model:effort"), TONYPI_STEER_MODE
(cut|steer), TONYPI_HARNESS_VOICE (stream|transcribe). Claude: CLAUDE_CODE_OAUTH_TOKEN in secrets.env.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import threading
import time
from concurrent.futures import Future
from pathlib import Path

from tonypi.agent.brain import STOP_RE
from tonypi.harness import backends, peripherals
from tonypi.harness.core import Harness

log = logging.getLogger("tonypi.harness")
SOCK = os.environ.get("TONYPI_HARNESS_SOCK", "/run/tonypi/harness.sock")
SECRETS = Path(os.environ.get("TONYPI_SECRETS", "/home/pi/.config/tonypi/secrets.env"))


def load_secrets(path: Path = SECRETS) -> None:
    """KEY=VALUE lines into os.environ (the environment wins)."""
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip("'\""))


class Frontend:
    def __init__(self, voice: bool, sound: bool, brain: str | None = None) -> None:
        from tonypi.tonyd.client import TonydClient

        self.clients: set[asyncio.StreamWriter] = set()
        self.loop: asyncio.AbstractEventLoop | None = None
        self.ask_fut: Future | None = None  # ask_user without the voice service: the next typed line answers
        self.speaker = None
        self.link = None
        # voice: the shared tonypi-voice service (GPT-Live-1) when it runs, else the local "hey Tony" listener
        from tonypi.harness.peripherals.voicelink import VoiceLink
        use_service = voice and os.environ.get("TONYPI_HARNESS_VOICE_SERVICE", "auto") != "off" and (
            VoiceLink.wanted() or os.environ.get("TONYPI_HARNESS_VOICE_SERVICE") == "on")
        if use_service:
            self.link = VoiceLink(self._from_voice, lambda: self.emit("info", text="barge-in"))
        elif sound or voice:
            from tonypi.agent.speech import Speaker
            self.speaker = Speaker()
        self.tonyd = TonydClient(on_event=self._on_tonyd_event)
        names = os.environ.get("TONYPI_PERIPHERALS", "body,camera,voice,learning,iphone").split(",")
        self.peripherals = peripherals.build([n.strip() for n in names if n.strip()], tonyd=self.tonyd,
                                             speaker=self.speaker, on_say=lambda s: self.emit("say", text=s),
                                             voicelink=self.link, ask_typed=self._ask_typed)
        self.harness = Harness(self.peripherals, backends.build(brain), emit=self.emit,
                               on_task_done=self._task_done)
        self.learning = next((p for p in self.peripherals if p.name == "learning"), None)
        if self.learning:
            self.learning.attach(self.harness)
        self.listener = None
        if voice and not self.link:
            from tonypi.agent.speech import Listener
            # streaming speech-to-text only: the LFM3 second pass would load the shared llama-server, and a cloud
            # brain copes with the odd misheard word (TONYPI_HARNESS_VOICE=transcribe brings it back)
            self.listener = Listener(self.speaker, lambda: self.emit("wake"),
                                     lambda text, audio=None, recording=None: self._from_thread(text, "voice"),
                                     mode=os.environ.get("TONYPI_HARNESS_VOICE", "stream"))

    # ---- events -----------------------------------------------------------------------------------------------

    def emit(self, type_: str, **kw) -> None:
        ev = {"type": type_, "t": round(time.time(), 3), **kw}
        if self.loop is None:
            return
        if threading.current_thread() is not threading.main_thread():
            self.loop.call_soon_threadsafe(self._broadcast, ev)
        else:
            self._broadcast(ev)

    def _broadcast(self, ev: dict) -> None:
        log.info("%s %s", ev["type"], {k: v for k, v in ev.items() if k not in ("type", "t")})
        line = (json.dumps(ev, default=str) + "\n").encode()
        for w in list(self.clients):
            try:
                w.write(line)
            except Exception:
                self.clients.discard(w)

    def _on_tonyd_event(self, ev: dict) -> None:
        if ev.get("event") == "warn":  # tonyd watchdog: low battery, overheating
            self.emit("info", text=ev["text"])
            if not self.harness.busy():
                self.harness.say(ev["text"])

    def _from_thread(self, text: str, source: str) -> None:
        asyncio.run_coroutine_threadsafe(self.harness.submit(text, source), self.loop)

    def _from_voice(self, voice_id: int, text: str) -> None:
        """A request from the voice service: a task (or steering for the running one); 'stop' is handled at once."""
        async def go() -> None:
            await self.harness.submit(text, "voice")
            if self.harness.busy():
                self.link.task_started(voice_id)  # answered (final) when the task ends
            else:
                self.link.close(voice_id)
        asyncio.run_coroutine_threadsafe(go(), self.loop)

    def _ask_typed(self, question: str, timeout: float) -> str | None:
        """ask_user on the text socket: the question goes out as an `ask` event, the next typed line answers."""
        fut: Future = Future()
        self.ask_fut = fut
        self.emit("ask", text=question)
        try:
            return fut.result(timeout)
        except Exception:
            return None
        finally:
            self.ask_fut = None

    def _task_done(self, source: str) -> None:
        if self.learning and self.learning.reload_pending:  # self-update committed: hot reload once idle
            self.loop.call_later(1.0, self._reload_when_idle)
        if source == "voice" and self.listener and not self.harness.stopped:
            voice = self.harness.peripherals.get("voice")

            def later() -> None:  # the follow-up window opens once Tony has finished talking
                if voice:
                    voice.wait_idle()
                self.listener.followup()

            threading.Thread(target=later, daemon=True).start()

    def _reload_when_idle(self) -> None:
        voice = self.harness.peripherals.get("voice")
        if self.harness.busy() or (voice and not voice.idle()):
            self.loop.call_later(1.0, self._reload_when_idle)
            return
        from tonypi.harness.learning import reexec
        self.emit("info", text="reloading with the new code")
        if self.listener:
            self.listener.stop()
        reexec()

    # ---- socket -----------------------------------------------------------------------------------------------

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.clients.add(writer)
        try:
            while line := await reader.readline():
                line = line.decode().strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    msg = line
                text = msg.get("text", "") if isinstance(msg, dict) else str(msg)
                if self.ask_fut is not None and not self.ask_fut.done() and text and text != "/status" and not (
                        isinstance(msg, dict) and msg.get("op") == "say") and not STOP_RE.match(text):  # an answer
                    self.ask_fut.set_result(text)
                    writer.write((json.dumps({"type": "accepted", "op": "answer"}) + "\n").encode())
                    continue
                if isinstance(msg, dict) and msg.get("op") == "say":
                    self.harness.say(text)
                    writer.write((json.dumps({"type": "accepted", "op": "say"}) + "\n").encode())
                elif text == "/status":
                    st = await asyncio.to_thread(self.harness.status)
                    self.emit("status", **st)
                else:
                    rid = await self.harness.submit(text, msg.get("source", "text") if isinstance(msg, dict)
                                                    else "text")
                    writer.write((json.dumps({"type": "accepted", "id": rid}) + "\n").encode())
        except (ConnectionError, OSError):
            pass
        finally:
            self.clients.discard(writer)
            writer.close()

    async def run(self) -> None:
        self.loop = asyncio.get_running_loop()
        await self.harness.start()
        path = Path(SOCK)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.unlink(missing_ok=True)
        server = await asyncio.start_unix_server(self._client, str(path))
        os.chmod(path, 0o660)
        log.info("listening on %s; brains: %s; tools: %s", path,
                 [b.name for b in self.harness.backends if b.configured()], list(self.harness.tools))
        if self.listener:
            self.listener.start()
        if self.speaker:
            self.harness.say("Tony is ready.")
        async with server:
            await server.serve_forever()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--voice", action="store_true", help='listen for "hey Tony" (stop tonypi-agent first)')
    ap.add_argument("--mute", action="store_true", help="no speaker: replies only as events")
    ap.add_argument("--brain", help="backend order, e.g. claude,local or local (default TONYPI_BRAIN)")
    ap.add_argument("--check", action="store_true", help="print the E2E readiness checklist and exit")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    load_secrets()
    if args.check:
        from tonypi.harness import readiness
        raise SystemExit(readiness.run())
    asyncio.run(Frontend(voice=args.voice, sound=not args.mute, brain=args.brain).run())


def chat() -> None:
    """tonypi-harness-chat: tonypi-chat pointed at the harness socket."""
    os.environ["TONYPI_SOCK"] = SOCK
    from tonypi.agent import chat as agent_chat
    agent_chat.main()


if __name__ == "__main__":
    main()
