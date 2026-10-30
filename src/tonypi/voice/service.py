"""tonypi-voice: Tony's ears and mouth for whichever harness (brain) is attached.

Owns the USB mic + speaker. Two modes, same socket interface:
- gpt-live: GPT-Live-1 (`gpt-live-1-codex`) through `codex app-server` realtime v3 over WebRTC (aiortc), on the
  ChatGPT login (docs/GPT_LIVE.md). GPT-Live has no tools: every user request arrives as a *delegation*, which goes
  to the attached brain as a `heard` event; the brain's `reply` text is appended to that delegation and spoken.
- local: "hey Tony" wake word + streaming ASR (tonypi.agent.speech.Listener) and Piper TTS, when GPT-Live is down.

Socket /run/tonypi/voice.sock, JSON lines:
  -> {"op":"attach","name":"pi"}                    become the active brain (last attach wins; the old one is told)
  <- {"event":"heard","id":N,"text":"..."}          a request from the user
  -> {"op":"reply","id":N,"text":"...","final":b}   speak a result (final) or a progress note
  -> {"op":"say","text":"..."}                      speak unprompted
  -> {"op":"ask","text":"...","timeout_s":30}       ask the user out loud; reply {"answer": text | null}
  <- {"event":"barge_in"}                           the user talked over Tony
  <- {"event":"transcript","text":"..."}            what GPT-Live heard (informational)
  -> {"op":"status"}
Never moves the robot: "stop" is just a heard request; the brain stops tonyd itself.
"""

from __future__ import annotations

import argparse
import asyncio
import fractions
import itertools
import queue
import re
import threading
import json
import logging
import os
import time
from pathlib import Path

import numpy as np

from tonypi.voice import SOCK

log = logging.getLogger("tonypi.voice")

ALSA_DEV = os.environ.get("TONYPI_ALSA", "plughw:0,0")
MIC_RATE, OUT_RATE = 16000, 48000
MIC_CHUNK = 320  # 20 ms at 16 kHz
OUT_CHUNK = 960  # 20 ms at 48 kHz
CODEX = os.environ.get("TONYPI_CODEX", "/home/pi/.local/bin/codex")
CODEX_HOME = os.environ.get("TONYPI_VOICE_CODEX_HOME", "/home/pi/.config/tonypi/codex-voice")
VOICE = os.environ.get("TONYPI_VOICE_NAME", "cove")
RETRY_LIVE_S = 120
# No echo cancellation on the USB speaker/mic: by default the mic sends silence while Tony talks (half duplex), so
# GPT-Live doesn't hear (and transcribe, or act on) Tony's own voice. 1 = full duplex (barge-in while Tony talks).
DUPLEX = os.environ.get("TONYPI_VOICE_DUPLEX", "0") == "1"
APPEND_MAX = 450  # Codex chunks context appends at 500 bytes

LIVE_PROMPT = (
    "You are the voice of Tony, a small humanoid robot. Another system, the robot's brain, does all the thinking and "
    "acting. Delegate to the brain every request, question or correction a robot could act on or answer (move, look, "
    "find, walk, stop, 'actually turn around', questions about what Tony sees or how he is, answers to Tony's "
    "questions), passing the user's words as closely as possible. Treat such requests as meant for Tony even when his "
    "name is missing or misheard (it often comes out as 'Ciao', 'Tell me', 'Toni', 'Hey'). Ignore only speech clearly "
    "meant for someone else: long dictation to a computer, phone calls, people talking to each other about unrelated "
    "things, the TV. Stay silent for those. Never answer from your own knowledge and never claim something was done; "
    "at most say a very short acknowledgement ('On it.') while the brain works. When the brain's text arrives, say it "
    "naturally and briefly, in the first person as Tony. If the brain asks the user a question, ask it.")


def resample(x: np.ndarray, src: int, dst: int) -> np.ndarray:
    if src == dst or not len(x):
        return x
    n = int(round(len(x) * dst / src))
    return np.interp(np.linspace(0, len(x) - 1, n), np.arange(len(x)), x.astype(np.float32)).astype(np.int16)


class Audio:
    """One arecord (16 kHz) and one aplay (48 kHz) for the whole service; a 20 ms clocked mixer for the speaker."""

    def __init__(self) -> None:
        self.mic_q: asyncio.Queue[bytes] = asyncio.Queue(maxsize=50)
        self.mic_listeners: list = []  # callables(bytes) also fed every mic chunk (local mode)
        self.remote = np.zeros(0, np.int16)  # GPT-Live's voice, 48 kHz
        self.local = np.zeros(0, np.int16)   # Piper, resampled to 48 kHz
        self.mute_mic_until = 0.0  # don't let GPT-Live hear Tony's own Piper speech
        self.inject = bytearray()
        self.voice_until = 0.0
        self.mic_chunks = 0  # 20 ms chunks read from the mic
        self.sent_chunks = 0  # chunks handed to GPT-Live
        self.spoken_s = {"gpt-live": 0.0, "piper": 0.0}  # seconds of speech played, for status / tests  # test audio that replaces the mic (op inject: "the user said ...")

    async def _open_mic(self) -> None:
        self.rec = await asyncio.create_subprocess_exec(
            "arecord", "-q", "-D", ALSA_DEV, "-f", "S16_LE", "-r", str(MIC_RATE), "-c", "1", "-t", "raw",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        asyncio.create_task(self._drain(self.rec.stderr, "arecord"))

    @staticmethod
    async def _drain(stream, name: str) -> None:
        """Log a child's stderr (an unread pipe would fill up and stall it)."""
        async for line in stream:
            log.warning("%s: %s", name, line.decode(errors="replace").strip())

    async def start(self) -> None:
        await self._open_mic()
        self.play = await asyncio.create_subprocess_exec(
            "aplay", "-q", "-D", ALSA_DEV, "-f", "S16_LE", "-r", str(OUT_RATE), "-c", "1", "-t", "raw",
            "--buffer-time=200000", stdin=asyncio.subprocess.PIPE)
        asyncio.create_task(self._read_mic())
        asyncio.create_task(self._mix())

    async def _read_mic(self) -> None:
        """Never dies: if arecord exits (device busy right after another owner let go, USB hiccup) it is reopened."""
        while True:
            try:
                buf = await self.rec.stdout.readexactly(MIC_CHUNK * 2)
            except (asyncio.IncompleteReadError, OSError) as e:
                log.warning("mic stopped (%s, arecord exit %s); reopening in 0.5 s", type(e).__name__,
                            self.rec.returncode)
                await asyncio.sleep(0.5)
                await self._open_mic()
                continue
            self.mic_chunks += 1
            if self.inject:
                buf, self.inject[:] = bytes(self.inject[:len(buf)]).ljust(len(buf), b"\0"), self.inject[len(buf):]
            elif time.monotonic() < self.mute_mic_until or (not DUPLEX and time.monotonic() < self.voice_until + 0.25):
                buf = bytes(len(buf))
            for f in self.mic_listeners:
                f(buf)
            if self.mic_q.full():
                self.mic_q.get_nowait()
            self.mic_q.put_nowait(buf)

    def add_remote(self, pcm: np.ndarray) -> None:
        self.remote = np.concatenate([self.remote[-OUT_RATE * 2:], pcm])  # at most ~2 s behind
        if np.abs(pcm).max(initial=0) > 300:  # not just the silence frames WebRTC keeps sending
            self.spoken_s["gpt-live"] += len(pcm) / OUT_RATE
            self.voice_until = time.monotonic() + len(self.remote) / OUT_RATE  # when Tony stops talking

    def add_local(self, pcm48: np.ndarray) -> None:
        self.local = np.concatenate([self.local, pcm48])
        self.spoken_s["piper"] += len(pcm48) / OUT_RATE
        self.mute_mic_until = max(self.mute_mic_until, time.monotonic()) + len(pcm48) / OUT_RATE + 0.4

    @property
    def speaking_local(self) -> bool:
        return len(self.local) > 0

    async def _mix(self) -> None:
        t = time.monotonic()
        while True:
            a, self.remote = self.remote[:OUT_CHUNK], self.remote[OUT_CHUNK:]
            b, self.local = self.local[:OUT_CHUNK], self.local[OUT_CHUNK:]
            out = np.zeros(OUT_CHUNK, np.int32)
            out[:len(a)] += a
            out[:len(b)] += b
            self.play.stdin.write(np.clip(out, -32768, 32767).astype(np.int16).tobytes())
            t += OUT_CHUNK / OUT_RATE
            await asyncio.sleep(max(0.0, t - time.monotonic()))


class Piper:
    def __init__(self) -> None:
        from piper import PiperVoice

        from tonypi.agent.speech import VOICE as VOICE_MODEL

        self.voice = PiperVoice.load(str(VOICE_MODEL))
        self.rate = self.voice.config.sample_rate

    def synth(self, text: str) -> np.ndarray:
        pcm = b"".join(c.audio_int16_bytes for c in self.voice.synthesize(text))
        return resample(np.frombuffer(pcm, np.int16), self.rate, OUT_RATE)


class Live:
    """GPT-Live-1 via codex app-server realtime v3 (WebRTC). Calls back into the Hub."""

    def __init__(self, hub: "Hub") -> None:
        self.hub, self.audio = hub, hub.audio
        self.proc = None
        self.pc = self.dc = None
        self.tid = None
        self.ready = asyncio.Event()
        self.closed = asyncio.Event()
        self._pending: dict[int, asyncio.Future] = {}
        self._ids = itertools.count(1)

    async def _rpc(self, method: str, params: dict | None = None, timeout: float = 30) -> dict:
        n = next(self._ids)
        fut = asyncio.get_running_loop().create_future()
        self._pending[n] = fut
        self.proc.stdin.write((json.dumps({"id": n, "method": method, "params": params or {}}) + "\n").encode())
        await self.proc.stdin.drain()
        msg = await asyncio.wait_for(fut, timeout)
        if "error" in msg:
            raise RuntimeError(f"{method}: {msg['error']}")
        return msg.get("result") or {}

    async def _reader(self, sdp_q: asyncio.Queue) -> None:
        async for line in self.proc.stdout:
            if not line.strip():
                continue
            try:
                m = json.loads(line)
            except ValueError:
                continue
            if "id" in m and m["id"] in self._pending:
                self._pending.pop(m["id"]).set_result(m)
                continue
            meth = m.get("method", "")
            if meth == "thread/realtime/sdp":
                await sdp_q.put(m["params"]["sdp"])
            elif meth in ("thread/realtime/closed", "thread/realtime/error"):
                log.warning("%s %s", meth, json.dumps(m.get("params"))[:300])
                self.closed.set()
        self.closed.set()

    async def start(self) -> None:
        from aiortc import MediaStreamTrack, RTCPeerConnection, RTCSessionDescription
        import av

        env = {k: v for k, v in os.environ.items() if k not in ("OPENAI_API_KEY", "CODEX_API_KEY")}
        env["CODEX_HOME"] = CODEX_HOME
        self.proc = await asyncio.create_subprocess_exec(
            CODEX, "app-server", "--listen", "stdio://", "--enable", "realtime_conversation",
            "-c", 'model_provider="openai"', stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, env=env, limit=1 << 24)
        sdp_q: asyncio.Queue = asyncio.Queue()
        asyncio.create_task(self._reader(sdp_q))
        await self._rpc("initialize", {"clientInfo": {"name": "tonypi-voice", "version": "1"},
                                       "capabilities": {"experimentalApi": True}})
        self.proc.stdin.write(b'{"method":"initialized","params":{}}\n')
        self.tid = (await self._rpc("thread/start", {"cwd": "/tmp", "ephemeral": True}))["thread"]["id"]

        audio = self.audio

        class Mic(MediaStreamTrack):
            kind = "audio"

            def __init__(self) -> None:
                super().__init__()
                self.pts = 0

            async def recv(self):
                buf = await audio.mic_q.get()
                audio.sent_chunks += 1
                f = av.AudioFrame(format="s16", layout="mono", samples=MIC_CHUNK)
                f.planes[0].update(buf)
                f.sample_rate, f.pts, f.time_base = MIC_RATE, self.pts, fractions.Fraction(1, MIC_RATE)
                self.pts += MIC_CHUNK
                return f

        self.pc = RTCPeerConnection()
        self.pc.addTrack(Mic())
        self.dc = self.pc.createDataChannel("oai-events")
        rs = av.AudioResampler(format="s16", layout="mono", rate=OUT_RATE)

        @self.pc.on("track")
        def on_track(track):
            async def pull():
                while True:
                    try:
                        frame = await track.recv()
                    except Exception:
                        return
                    for f in rs.resample(frame):
                        audio.add_remote(f.to_ndarray().reshape(-1).astype(np.int16))
            asyncio.create_task(pull())

        @self.dc.on("open")
        def on_open():
            asyncio.get_running_loop().call_later(8.0, self.ready.set)  # fallback if session.started never shows

        @self.dc.on("message")
        def on_msg(raw):
            try:
                ev = json.loads(raw)
            except ValueError:
                return
            typ = ev.get("type", "")
            if typ == "delegation.created":
                item = ev.get("item") or {}
                text = " ".join(c.get("text", "") for c in item.get("content", []) if isinstance(c, dict)).strip()
                self.hub.on_request(text, item.get("id"))
            elif typ == "input_transcript.added":
                self.hub.on_transcript((ev.get("item") or {}).get("text", ""))
            elif typ == "output_audio_buffer.cleared":
                audio.remote = np.zeros(0, np.int16)
                self.hub.emit_attached({"event": "barge_in"})
            elif typ == "error":
                log.warning("live error: %s", json.dumps(ev)[:300])
            elif typ == "session.started":
                # audio sent before this is lost (the first commands after a start were never heard): ready a bit later
                asyncio.get_running_loop().call_later(1.0, self.ready.set)
            elif typ == "session.closed":
                self.closed.set()

        await self.pc.setLocalDescription(await self.pc.createOffer())
        await self._rpc("thread/realtime/start", {
            "threadId": self.tid, "outputModality": "audio", "version": "v3",
            "transport": {"type": "webrtc", "sdp": self.pc.localDescription.sdp}, "voice": VOICE,
            "prompt": LIVE_PROMPT, "realtimeStartInstructions": "Reply skip. Do not use tools.",
            "clientManagedHandoffs": True})
        answer = await asyncio.wait_for(sdp_q.get(), 30)
        await self.pc.setRemoteDescription(RTCSessionDescription(answer, "answer"))
        await asyncio.wait_for(self.ready.wait(), 30)
        log.info("GPT-Live session up (voice %s)", VOICE)

    def _send(self, ev: dict) -> bool:
        if self.dc is None or self.dc.readyState != "open":
            return False
        self.dc.send(json.dumps(ev))
        return True

    def append(self, item_id: str, text: str, speak: bool = True) -> bool:
        ok = True
        for i in range(0, len(text), APPEND_MAX):
            ok &= self._send({"type": "delegation.context.append", "delegation_item_id": item_id,
                              "channel": "speakable" if speak else "commentary",
                              "content": [{"type": "input_text", "text": text[i:i + APPEND_MAX]}]})
        return ok

    def speak(self, text: str) -> bool:
        """Unprompted speech (no open delegation): session-level speakable context."""
        return self._send({"type": "session.context.append", "channel": "speakable",
                           "content": [{"type": "input_text", "text":
                                        f"Say exactly these words to the user now, as Tony, and add nothing: {text[:APPEND_MAX - 70]}"}]})

    async def close(self) -> None:
        try:
            if self.tid:
                await self._rpc("thread/realtime/stop", {"threadId": self.tid}, timeout=5)
        except Exception:
            pass
        if self.pc:
            await self.pc.close()
        if self.proc and self.proc.returncode is None:
            self.proc.terminate()


class Hub:
    def __init__(self, live_enabled: bool = True) -> None:
        self.audio = Audio()
        self.piper: Piper | None = None
        self.live: Live | None = None
        self.live_enabled = live_enabled
        self.mode = "starting"
        self.attached: tuple[str, asyncio.StreamWriter] | None = None
        self.requests: dict[int, str | None] = {}  # heard id -> GPT-Live delegation item id (None in local mode)
        self.ids = itertools.count(1)
        self.ask_fut: asyncio.Future | None = None
        self.ask_parts: list[str] = []
        self.ask_timer: asyncio.TimerHandle | None = None
        self.ask_listen_from = 0.0  # ignore the transcript of Tony's own question
        self.listener = None
        self.loop: asyncio.AbstractEventLoop | None = None

    # ---- events to the brain ---------------------------------------------------------------------------------

    def emit_attached(self, ev: dict) -> None:
        if self.attached:
            try:
                self.attached[1].write((json.dumps(ev) + "\n").encode())
            except Exception:
                self.attached = None

    def on_transcript(self, text: str) -> None:
        """What GPT-Live heard. An answer to an `ask` is often not delegated (it isn't a request), so a pending ask
        collects the transcript and resolves after a 1.2 s pause."""
        self.emit_attached({"event": "transcript", "text": text})
        log.info("transcript %r", text)
        if (not text or self.ask_fut is None or self.ask_fut.done()
                or time.monotonic() < max(self.ask_listen_from, self.audio.voice_until + 0.3)):  # not Tony's own question
            return
        self.ask_parts.append(text)
        if self.ask_timer:
            self.ask_timer.cancel()
        self.ask_timer = asyncio.get_running_loop().call_later(1.2, self._resolve_ask_from_transcript)

    def _resolve_ask_from_transcript(self) -> None:
        if self.ask_fut is not None and not self.ask_fut.done() and self.ask_parts:
            self.ask_fut.set_result(re.sub(r"\s+([.,!?])", r"\1", " ".join("".join(self.ask_parts).split())))

    def on_request(self, text: str, item_id: str | None) -> None:
        """A user utterance: answers a pending ask, else goes to the attached brain."""
        log.info("heard (%s): %r", self.mode, text)
        if self.ask_fut is not None and not self.ask_fut.done():
            self.ask_fut.set_result(text)
            if item_id and self.live:
                self.live.append(item_id, "(Tony's brain got your answer.)", speak=False)
            return
        if self.attached is None:
            self._speak_or_append(item_id, "My brain isn't connected right now.")
            return
        rid = next(self.ids)
        self.requests[rid] = item_id
        self.emit_attached({"event": "heard", "id": rid, "text": text, "mode": self.mode})

    # ---- speaking ---------------------------------------------------------------------------------------------

    def _speak_or_append(self, item_id: str | None, text: str) -> None:
        if item_id and self.live and self.live.append(item_id, text):
            return
        if self.mode == "gpt-live" and self.live and self.live.speak(text):
            return
        asyncio.create_task(self.say_local(text))

    async def say_local(self, text: str) -> None:
        if self.piper is None:
            self.piper = await asyncio.to_thread(Piper)
        pcm = await asyncio.to_thread(self.piper.synth, text)
        self.audio.add_local(pcm)

    # ---- local fallback ---------------------------------------------------------------------------------------

    async def start_local(self) -> None:
        if self.listener is not None:
            return
        from tonypi.agent import speech

        class _Quiet:  # Listener only needs speaker.quiet_until
            quiet_until = 0.0

        loop = asyncio.get_running_loop()
        lis = await asyncio.to_thread(
            speech.Listener, _Quiet(), lambda: None,
            lambda text, *a, **k: loop.call_soon_threadsafe(self.on_request, text, None), mode="stream")
        st = lis.new_state()
        buf = bytearray()
        q: "queue.Queue[np.ndarray]" = queue.Queue(maxsize=100)

        def feed(chunk: bytes) -> None:  # in the event loop: batch to 0.1 s, recognise in a thread (keeps audio smooth)
            buf.extend(chunk)
            if len(buf) >= 3200:
                x = np.frombuffer(bytes(buf), np.int16).astype(np.float32) / 32768
                buf.clear()
                if self.mode == "local" and not self.audio.speaking_local and not q.full():
                    q.put_nowait(x)

        def recognise() -> None:
            while True:
                lis.feed(st, q.get())

        threading.Thread(target=recognise, name="local-asr", daemon=True).start()
        self.listener = lis
        self.audio.mic_listeners.append(feed)
        log.info("local voice ready (say 'hey Tony')")

    async def run_live(self) -> None:
        while self.live_enabled:
            self.live = Live(self)
            try:
                await self.live.start()
                self.mode = "gpt-live"
                await self.live.closed.wait()
                log.warning("GPT-Live session closed; restarting")
            except Exception as e:
                log.warning("GPT-Live unavailable (%s): local voice for now, retry in %d s", e, RETRY_LIVE_S)
            await self.live.close()
            self.live, self.mode = None, "local"
            await asyncio.sleep(RETRY_LIVE_S)

    # ---- socket -----------------------------------------------------------------------------------------------

    async def handle(self, msg: dict, writer: asyncio.StreamWriter) -> dict:
        op = msg.get("op")
        if op == "attach":
            old = self.attached
            if old and old[1] is not writer:
                try:
                    old[1].write(b'{"event":"detached"}\n')
                except Exception:
                    pass
            self.attached = (str(msg.get("name", "?")), writer)
            log.info("brain attached: %s", self.attached[0])
            return {"attached": self.attached[0], "mode": self.mode}
        if op == "reply":
            rid = msg.get("id")
            item_id = self.requests.get(rid) if rid is not None else None
            text = str(msg.get("text") or "").strip()
            if text:
                self._speak_or_append(item_id, text)
            if msg.get("final", True) and rid in self.requests:
                self.requests.pop(rid, None)
                if self.listener is not None and self.mode == "local":
                    self.listener.followup()
            return {"spoken": bool(text)}
        if op == "say":
            self._speak_or_append(None, str(msg.get("text") or ""))
            return {"spoken": True}
        if op == "ask":
            self.ask_fut = asyncio.get_running_loop().create_future()
            self.ask_parts, self.ask_listen_from = [], time.monotonic() + 1.0
            self._speak_or_append(None, str(msg.get("text") or ""))
            if self.listener is not None and self.mode == "local":
                await asyncio.sleep(0.2)
                while self.audio.speaking_local:
                    await asyncio.sleep(0.1)
                self.listener.followup()
            try:
                answer = await asyncio.wait_for(asyncio.shield(self.ask_fut), float(msg.get("timeout_s", 30)))
            except asyncio.TimeoutError:
                answer = None
            self.ask_fut = None
            return {"answer": answer}
        if op == "inject":  # tests: play a WAV into the mic stream as if the user said it
            import wave

            with wave.open(str(msg["wav"])) as w:
                pcm = np.frombuffer(w.readframes(w.getnframes()), np.int16)
                if w.getnchannels() > 1:
                    pcm = pcm.reshape(-1, w.getnchannels())[:, 0]
                pcm = resample(pcm, w.getframerate(), MIC_RATE)
            self.audio.inject.extend(pcm.tobytes() + bytes(MIC_RATE))  # + 0.5 s of silence after it
            return {"seconds": round(len(pcm) / MIC_RATE, 2)}
        if op == "status":
            return {"mode": self.mode, "attached": self.attached[0] if self.attached else None,
                    "voice": VOICE if self.mode == "gpt-live" else "piper", "open_requests": len(self.requests),
                    "spoken_s": {k: round(v, 1) for k, v in self.audio.spoken_s.items()},
                    "mic_s": round(self.audio.mic_chunks * MIC_CHUNK / MIC_RATE, 1),
                    "sent_to_live_s": round(self.audio.sent_chunks * MIC_CHUNK / MIC_RATE, 1)}
        if op == "ping":
            return {"pong": True}
        raise ValueError(f"unknown op {op!r}")

    async def client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            async for line in reader:
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                rid = msg.get("rid")

                async def answer(m=msg, rid=rid):
                    try:
                        out = {"ok": True, **(await self.handle(m, writer))}
                    except Exception as e:
                        out = {"ok": False, "error": str(e)}
                    if rid is not None:
                        out["rid"] = rid
                    writer.write((json.dumps(out) + "\n").encode())

                asyncio.create_task(answer())  # an `ask` must not block the brain's other requests
        finally:
            if self.attached and self.attached[1] is writer:
                log.info("brain detached: %s", self.attached[0])
                self.attached = None

    async def serve(self, path: str = SOCK) -> None:
        Path(path).unlink(missing_ok=True)
        server = await asyncio.start_unix_server(self.client, path=path)
        os.chmod(path, 0o666)
        log.info("listening on %s", path)
        async with server:
            await server.serve_forever()

    async def main(self) -> None:
        await self.audio.start()
        await self.start_local()
        self.mode = "local"
        tasks = [asyncio.create_task(self.serve())]
        if self.live_enabled:
            tasks.append(asyncio.create_task(self.run_live()))
        await asyncio.gather(*tasks)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--no-live", action="store_true", help="local wake word + Piper only (no GPT-Live)")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    asyncio.run(Hub(live_enabled=not args.no_live).main())


if __name__ == "__main__":
    main()
