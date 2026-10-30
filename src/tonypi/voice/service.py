"""tonypi-voice: Tony's ears, mouth and (in live mode, the default) mind.

Owns the USB mic + speaker. Two modes:
- gpt-live: GPT-Live-1 (`gpt-live-1-codex`) through `codex app-server` realtime v3 over WebRTC (aiortc), on the
  ChatGPT login (docs/GPT_LIVE.md). In live mode (TONYPI_VOICE_MODE=live, the default) GPT-Live's own Codex thread is
  Tony's brain, with background tasks and autonomy sessions (live.py, docs/BRAIN.md).
- local: "hey Tony" wake word + streaming ASR (tonypi.agent.speech.Listener) and Piper TTS, while GPT-Live is down:
  it handles only stop and "switch to local" (the offline LFM3 agent, tonypi-agent).

Socket /run/tonypi/voice.sock, JSON lines ({"op", "rid"?, ...} -> {"ok", "rid", ...}):
  {"op":"say","text":"..."}                          speak unprompted
  {"op":"text","text":"...","source":"typed"}        a typed request for the brain (live mode; phone bridge: phone_mic)
  {"op":"status"} / {"op":"tasks"} / {"op":"ping"}
  {"op":"inject","wav":"/path.wav"}                  tests: play a WAV into the mic as if the user said it
"Stop" never depends on a model: the transcript reflex stops tonyd itself (with a latch in live mode).
"""

from __future__ import annotations

import argparse
import asyncio
import collections
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

from tonypi import switch
from tonypi.voice import SOCK

log = logging.getLogger("tonypi.voice")

ALSA_DEV = os.environ.get("TONYPI_ALSA", "plughw:0,0")
MIC_RATE, OUT_RATE = 16000, 48000
MIC_CHUNK = 320  # 20 ms at 16 kHz
OUT_CHUNK = 960  # 20 ms at 48 kHz
# Half duplex: the mic stays muted this long after Tony stops talking. The speaker (aplay 200 ms buffer) and mic
# (arecord) buffers delay sound, so at 0.25 s the tail of a short "Yep" reached GPT-Live as user speech and it
# answered itself in a loop ("Listening", "Yep", ... 2026-10-30).
ECHO_TAIL_S = 0.9
CODEX = os.environ.get("TONYPI_CODEX", "/home/pi/.local/bin/codex")
CODEX_HOME = os.environ.get("TONYPI_VOICE_CODEX_HOME", "/home/pi/.config/tonypi/codex-voice")
VOICE = os.environ.get("TONYPI_VOICE_NAME", "cove")
RETRY_LIVE_S = 120
# live: GPT-Live's own Codex agent is the brain, with full access (live.py). Anything else runs GPT-Live as a voice
# only, without a brain (tests, debugging).
VOICE_MODE = os.environ.get("TONYPI_VOICE_MODE", "live")
# No echo cancellation on the USB speaker/mic: by default the mic sends silence while Tony talks (half duplex), so
# GPT-Live doesn't hear (and transcribe, or act on) Tony's own voice. 1 = full duplex (barge-in while Tony talks).
DUPLEX = os.environ.get("TONYPI_VOICE_DUPLEX", "0") == "1"
APPEND_MAX = 450
# "stop" never depends on a model: GPT-Live sometimes treats a bare "Stop." as "stop talking" and delegates nothing
STOP_WORDS = re.compile(r"^\W*(?:hey\s+)?(?:tony\W+)?(stop|halt|freeze|stop it|stop moving)\b", re.I)  # Codex chunks context appends at 500 bytes
# The GPT-Live session runs only while someone talks to Tony: "Tony" / "hey Tony" (local keyword spotter) starts it, and
# it ends after this long with nothing going on (no user speech, no brain turn, no background task, Tony not talking).
# Tony's brain (the Codex app-server, thread L, tasks, autonomy) keeps running either way. 0 = never sleep.
VOICE_IDLE_S = float(os.environ.get("TONYPI_VOICE_IDLE_S", "30"))
PREROLL_CHUNKS = 100  # 2 s of mic kept before the wake word fired: GPT-Live hears "hey Tony, wave" whole
HOLD_MAX_CHUNKS = 1500  # 30 s: mic held while the session starts, then replayed into it
# Replay skips silence to catch up: a 20 ms chunk is silent below max(QUIET_RMS, 2.5 x the held audio's noise floor)
# (the mic runs at max gain: its floor is well above digital silence; a fixed threshold skipped nothing, 22 s behind)
QUIET_RMS = 100.0

# GPT-Live's fallback prompt (live.py loads prompts/live_voice.md). Keep the delegation wording: it delegated reliably
# in live tests (2026-10-29). Two rewrites that asked GPT-Live to judge whether speech was meant for Tony made it
# delegate nothing (it heard 'Tony' as 'Ciao').
LIVE_PROMPT = (
    "You are the voice of Tony, a small humanoid robot. Another system, the robot's brain, does all the thinking and "
    "acting. For EVERY thing the user says (requests, questions, corrections like 'actually turn around', 'stop', and "
    "answers to questions Tony asked) delegate it to the brain, passing the user's words as closely as possible. Never "
    "answer from your own knowledge and never claim something was done. Hand off silently: do not say an "
    "acknowledgement or waiting filler such as 'Okay', 'On it', 'Checking now' or 'Let me check'. Stay quiet while "
    "the brain works. When the brain's result arrives, say it naturally and briefly, in the first person as Tony. "
    "Still relay meaningful warnings, necessary questions and progress news that affects the user; never speak "
    "quiet commentary or add an acknowledgement before a result. If the brain asks the user a question, ask it.")


def _rms(buf: bytes) -> float:
    x = np.frombuffer(buf, np.int16).astype(np.float32)
    return float(np.sqrt(np.mean(x * x))) if len(x) else 0.0


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
        self.ring: collections.deque[bytes] = collections.deque(maxlen=PREROLL_CHUNKS)  # recent mic, for a wake
        # Wake word -> GPT-Live session: the mic is held (GPT-Live gets silence) until the session is ready, then the
        # held audio is replayed into it, long silences skipped so it catches up with the live mic.
        self.hold: collections.deque[bytes] | None = None
        self.hold_released = False
        self._quiet_run = 0
        self._quiet_rms = QUIET_RMS

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

    async def _open_play(self) -> None:
        self.play = await asyncio.create_subprocess_exec(
            "aplay", "-q", "-D", ALSA_DEV, "-f", "S16_LE", "-r", str(OUT_RATE), "-c", "1", "-t", "raw",
            "--buffer-time=200000", stdin=asyncio.subprocess.PIPE)

    async def start(self) -> None:
        await self._open_mic()
        await self._open_play()
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
            elif time.monotonic() < self.mute_mic_until or (not DUPLEX and time.monotonic() < self.voice_until + ECHO_TAIL_S):
                buf = bytes(len(buf))
            for f in self.mic_listeners:
                f(buf)
            self.ring.append(buf)
            if self.hold is not None:
                self.hold.append(buf)
                if self.hold_released:
                    continue  # the session's mic track reads the hold until it has caught up
                buf = bytes(len(buf))  # the session isn't ready: what it would hear now is replayed later
            if self.mic_q.full():
                self.mic_q.get_nowait()
            self.mic_q.put_nowait(buf)

    def start_hold(self) -> None:
        """The wake word fired: keep the last 2 s and everything from now on for the session that is starting."""
        if self.hold is None:
            self.hold = collections.deque(self.ring, maxlen=HOLD_MAX_CHUNKS)
            self.hold_released, self._quiet_run = False, 0

    def release_hold(self) -> None:
        """The session is ready: replay the held audio into it."""
        if self.hold is not None:
            self.hold_released = True
            if self.hold:
                floor = float(np.percentile([_rms(b) for b in self.hold], 20))
                self._quiet_rms = max(QUIET_RMS, 2.5 * floor)
            while not self.mic_q.empty():  # silence queued while it started
                self.mic_q.get_nowait()

    def drop_hold(self) -> None:
        self.hold, self.hold_released = None, False

    async def next_chunk(self) -> bytes:
        """The next 20 ms for GPT-Live: held audio first (paced at 1.25x, silences over 0.2 s skipped), then live."""
        while self.hold is not None and self.hold_released:
            if not self.hold:
                self.drop_hold()
                break
            buf = self.hold.popleft()
            quiet = _rms(buf) < self._quiet_rms
            self._quiet_run = self._quiet_run + 1 if quiet else 0
            if self._quiet_run > 10:
                continue
            await asyncio.sleep(0.016)
            return buf
        return await self.mic_q.get()

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
            if self.play.returncode is not None or self.play.stdin.is_closing():
                # the speaker went away (USB hiccup, 2026-10-30: Tony was mute for an hour): reopen it
                log.warning("speaker stopped (aplay exit %s); reopening in 1 s", self.play.returncode)
                await asyncio.sleep(1.0)
                try:
                    await self._open_play()
                except OSError as e:
                    log.warning("speaker reopen failed: %s", e)
                t = time.monotonic()
                continue
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
    """GPT-Live-1 via codex app-server realtime v3 (WebRTC). Calls back into the Hub.

    Two lifetimes: the app-server and its thread (start_brain .. closed: the process ended) and the realtime voice
    session on that thread (start_voice .. stop_voice / voice_closed), which comes and goes with the wake word."""

    def __init__(self, hub: "Hub") -> None:
        self.hub, self.audio = hub, hub.audio
        self.proc = None
        self.pc = self.dc = None
        self.tid = None
        self.codex_home = CODEX_HOME
        self.ready = asyncio.Event()
        self.closed = asyncio.Event()  # the app-server is gone
        self.voice_closed = asyncio.Event()  # the realtime session ended (or never started)
        self.voice_closed.set()
        self._sdp_q: asyncio.Queue = asyncio.Queue()
        self.voice_started_t = 0.0
        self._mark_ready = lambda what: None
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
            if "id" in m and "method" not in m and m["id"] in self._pending:
                self._pending.pop(m["id"]).set_result(m)
                continue
            meth = m.get("method", "")
            if "id" in m:  # a request from the app-server to us (tool call, approval, ...)
                asyncio.create_task(self._answer_server(m))
                continue
            if meth == "thread/realtime/sdp":
                await sdp_q.put(m["params"]["sdp"])
            elif meth in ("thread/realtime/closed", "thread/realtime/error"):
                log.warning("%s %s", meth, json.dumps(m.get("params"))[:300])
                self.voice_closed.set()
            else:
                if (meth == "thread/realtime/item/started"
                        and ((m.get("params") or {}).get("item") or {}).get("type") == "realtimeSessionStarted"):
                    self._mark_ready("started")  # live mode's data channel has no session.started: Codex says so
                try:
                    self.on_notification(meth, m.get("params") or {})
                except Exception:
                    log.exception("notification %s", meth)
        self.closed.set()

    async def _answer_server(self, m: dict) -> None:
        try:
            result = await self.on_server_request(m["method"], m.get("params") or {})
            out = {"id": m["id"], "result": result}
        except Exception as e:
            log.warning("server request %s failed: %s", m.get("method"), e)
            out = {"id": m["id"], "error": {"code": -32601, "message": str(e)}}
        self.proc.stdin.write((json.dumps(out) + "\n").encode())
        await self.proc.stdin.drain()

    # ---- hooks (LiveBrain in live.py overrides these) ------------------------------------------------------

    async def on_server_request(self, method: str, params: dict) -> dict:
        if method.endswith("requestApproval"):
            return {"decision": "accept"}
        raise ValueError(f"unsupported: {method}")

    def on_notification(self, method: str, params: dict) -> None:
        pass

    async def setup_thread(self) -> str:
        return (await self._rpc("thread/start", {"cwd": "/tmp", "ephemeral": True}))["thread"]["id"]

    def realtime_params(self, sdp: str) -> dict:
        return {"threadId": self.tid, "outputModality": "audio", "version": "v3",
                "transport": {"type": "webrtc", "sdp": sdp}, "voice": VOICE, "prompt": LIVE_PROMPT,
                "realtimeStartInstructions": "Reply skip. Do not use tools.", "clientManagedHandoffs": True,
                "delegationAckFiller": False}

    def on_dc_event(self, ev: dict) -> bool:
        """Return True if handled (the relay behaviour below is skipped)."""
        return False

    def busy(self) -> bool:
        """Work whose outcome the voice should still speak (LiveBrain: a brain turn, a background task)."""
        return False

    def last_activity(self) -> float:
        """monotonic time of the last thing that keeps the voice session awake."""
        return max(self.voice_started_t, self.audio.voice_until, getattr(self.hub, "last_heard_t", 0.0))

    @property
    def voice_on(self) -> bool:
        return self.dc is not None and self.dc.readyState == "open" and not self.voice_closed.is_set()

    async def start(self) -> None:
        await self.start_brain()
        await self.start_voice()

    async def start_brain(self) -> None:
        """The app-server and its thread; they stay up while the voice session comes and goes."""
        env = {k: v for k, v in os.environ.items() if k not in ("OPENAI_API_KEY", "CODEX_API_KEY")}
        env["CODEX_HOME"] = self.codex_home
        env["PATH"] = "/home/pi/.local/bin:/home/pi/tonypi/.venv/bin:" + env.get("PATH", "/usr/local/bin:/usr/bin:/bin")
        self.proc = await asyncio.create_subprocess_exec(
            CODEX, "app-server", "--listen", "stdio://", "--enable", "realtime_conversation",
            "-c", 'model_provider="openai"', stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, env=env, limit=1 << 24)
        asyncio.create_task(Audio._drain(self.proc.stderr, "codex"))
        asyncio.create_task(self._reader(self._sdp_q))
        await self._rpc("initialize", {"clientInfo": {"name": "tonypi-voice", "version": "2"},
                                       "capabilities": {"experimentalApi": True}})
        self.proc.stdin.write(b'{"method":"initialized","params":{}}\n')
        self.tid = await self.setup_thread()

    async def start_voice(self) -> None:
        """A GPT-Live realtime session on the thread (WebRTC); returns once it hears the mic."""
        from aiortc import MediaStreamTrack, RTCPeerConnection, RTCSessionDescription
        import av

        await self._close_pc()
        self.ready = asyncio.Event()
        self.voice_closed = asyncio.Event()
        while not self._sdp_q.empty():
            self._sdp_q.get_nowait()
        audio = self.audio

        class Mic(MediaStreamTrack):
            kind = "audio"

            def __init__(self) -> None:
                super().__init__()
                self.pts = 0

            async def recv(self):
                buf = await audio.next_chunk()
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

        ready = self.ready  # this session's: a late timer must not mark a newer session ready
        seen: set[str] = set()

        def mark_ready(what: str) -> None:
            # audio sent before the session started AND media flows (data channel open) is lost (the first commands
            # after a start were never heard; a replayed "hey Tony, ..." too): ready 1 s after both
            seen.add(what)
            if seen >= {"open", "started"}:
                asyncio.get_running_loop().call_later(1.0, ready.set)
        self._mark_ready = mark_ready

        @self.dc.on("open")
        def on_open():
            mark_ready("open")
            asyncio.get_running_loop().call_later(8.0, ready.set)  # fallback if no "started" ever shows

        @self.dc.on("message")
        def on_msg(raw):
            try:
                ev = json.loads(raw)
            except ValueError:
                return
            typ = ev.get("type", "")
            try:
                if self.on_dc_event(ev):
                    return
            except Exception:
                log.exception("dc event %s", typ)
            if typ == "delegation.created":
                item = ev.get("item") or {}
                text = " ".join(c.get("text", "") for c in item.get("content", []) if isinstance(c, dict)).strip()
                self.hub.on_request(text, item.get("id"))
            elif typ == "input_transcript.added":
                self.hub.on_transcript((ev.get("item") or {}).get("text", ""))
            elif typ == "output_audio_buffer.cleared":
                audio.remote = np.zeros(0, np.int16)
            elif typ == "error":
                log.warning("live error: %s", json.dumps(ev)[:300])
            elif typ == "session.started":
                mark_ready("started")
            elif typ == "session.closed":
                self.voice_closed.set()

        t = [time.monotonic()]
        await self.pc.setLocalDescription(await self.pc.createOffer())
        t.append(time.monotonic())
        params = self.realtime_params(self.pc.localDescription.sdp)
        t.append(time.monotonic())
        await self._rpc("thread/realtime/start", params)
        t.append(time.monotonic())
        answer = await asyncio.wait_for(self._sdp_q.get(), 30)
        t.append(time.monotonic())
        await self.pc.setRemoteDescription(RTCSessionDescription(answer, "answer"))
        await asyncio.wait_for(self.ready.wait(), 30)
        self.voice_started_t = time.monotonic()
        t.append(self.voice_started_t)
        log.info("GPT-Live session up in %.1f s (offer/params/start/answer/ready %s; voice %s)", t[-1] - t[0],
                 " ".join(f"{b - a:.1f}" for a, b in zip(t, t[1:])), VOICE)

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

    async def _close_pc(self) -> None:
        pc, self.pc, self.dc = self.pc, None, None
        if pc:
            try:
                await pc.close()
            except Exception as e:
                log.warning("closing the WebRTC connection: %s", e)
        self.audio.remote = np.zeros(0, np.int16)

    async def stop_voice(self) -> None:
        """End the realtime session; the thread and the app-server stay."""
        try:
            if self.tid and self.proc and self.proc.returncode is None and not self.voice_closed.is_set():
                await self._rpc("thread/realtime/stop", {"threadId": self.tid}, timeout=5)
        except Exception as e:
            log.warning("thread/realtime/stop: %s", e)
        self.voice_closed.set()
        await self._close_pc()

    async def close(self) -> None:
        await self.stop_voice()
        if self.proc and self.proc.returncode is None:
            self.proc.terminate()


class WakeSpotter:
    """The keyword spotter (sherpa-onnx, the same model as the local agent) that runs while the voice sleeps:
    "Tony" / "hey Tony" starts a GPT-Live session, "stop" / "freeze" / "halt" also stops the body at once."""

    WAKE = ["HEY TONY", "HI TONY", "OK TONY", "A TONY", "TONY"]
    STOP = ["STOP", "FREEZE", "HALT"]

    def __init__(self) -> None:
        import sherpa_onnx

        from tonypi.agent.speech import KWS_DIR, _keywords_file

        phrases = self.WAKE + self.STOP
        self.kws = sherpa_onnx.KeywordSpotter(
            tokens=str(KWS_DIR / "tokens.txt"),
            encoder=str(KWS_DIR / "encoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx"),
            decoder=str(KWS_DIR / "decoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx"),
            joiner=str(KWS_DIR / "joiner-epoch-12-avg-2-chunk-16-left-64.int8.onnx"),
            keywords_file=_keywords_file(KWS_DIR / "bpe.model", phrases), num_threads=1,
            keywords_score=float(os.environ.get("TONYPI_WAKE_SCORE", "2.5")),
            keywords_threshold=float(os.environ.get("TONYPI_WAKE_THRESHOLD", "0.15")))
        self.stream = self.kws.create_stream()

    def feed(self, samples: np.ndarray) -> str | None:
        """16 kHz float samples in; the keyword heard ("HEY_TONY", "STOP", ...) or None."""
        self.stream.accept_waveform(MIC_RATE, samples)
        while self.kws.is_ready(self.stream):
            self.kws.decode_stream(self.stream)
            if word := self.kws.get_result(self.stream):
                self.kws.reset_stream(self.stream)
                return word
        return None

    def reset(self) -> None:
        self.stream = self.kws.create_stream()


SWITCH_LABEL = {"local": "the local agent", "live": "live mode"}


def parse_switch(text: str) -> str | None:
    """Target of "switch to local" / "switch to live", else None (tonypi.switch matches loosely: ASR mangles both)."""
    target = switch.parse_switch(text)
    target = {"off": "local"}.get(target, target)
    return target if target in SWITCH_LABEL else None


class Hub:
    def __init__(self, live_enabled: bool = True) -> None:
        self.audio = Audio()
        self.piper: Piper | None = None
        self.live: Live | None = None
        self.live_enabled = live_enabled
        self.mode = "starting"
        self.utt_parts: list[str] = []
        self.utt_timer: asyncio.TimerHandle | None = None
        self.last_switch_t = -1e9
        self.last_stop_t = -1e9
        self.listener = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self.voice = "off"  # GPT-Live session: off (sleeping) | starting | on
        self.last_heard_t = 0.0  # monotonic: the last transcript piece GPT-Live heard
        self.wakes = 0  # wake words heard (status, tests)
        self.spotter: WakeSpotter | None = None
        self._voice_lock = asyncio.Lock()

    # ---- reflexes ------------------------------------------------------------------------------------------

    def on_transcript(self, text: str) -> None:
        """What GPT-Live heard, piece by piece: the stop reflex, and the whole utterance once it pauses (switch
        commands may never become a delegation: GPT-Live may find a misheard brain name meaningless)."""
        if STOP_WORDS.match("".join(self.utt_parts) + text):
            self._stop_body("".join(self.utt_parts) + text)
        self.last_heard_t = time.monotonic()
        self.utt_parts.append(text)
        if self.utt_timer:
            self.utt_timer.cancel()
        self.utt_timer = asyncio.get_running_loop().call_later(1.0, self._utterance_done)
        log.info("transcript %r", text)

    def _stop_body(self, text: str) -> None:
        if time.monotonic() - self.last_stop_t < 3:
            return
        self.last_stop_t = time.monotonic()
        log.warning("stop heard (%r): stopping tonyd directly", text)

        def stop() -> None:
            try:
                from tonypi.tonyd.client import TonydClient

                TonydClient().stop(latch=VOICE_MODE == "live")  # live: no brain may carry on moving
            except Exception as e:
                log.error("tonyd stop failed: %s", e)
        threading.Thread(target=stop, daemon=True).start()
        if VOICE_MODE == "live" and self.live is not None and hasattr(self.live, "interrupt_all"):
            asyncio.create_task(self.live.interrupt_all(body_only=True))

    def _utterance_done(self) -> None:
        text, self.utt_parts = "".join(self.utt_parts), []
        if (target := parse_switch(text)) is not None:
            self._switch(target, None)
        elif VOICE_MODE == "live" and text.strip() and not STOP_WORDS.match(text):
            if hasattr(self.live, "utterance_done"):
                self.live.utterance_done(" ".join(text.split()))

            def unlatch() -> None:  # the user spoke again after a stop: moving is allowed again
                try:
                    from tonypi.tonyd.client import TonydClient

                    TonydClient().unlatch()
                except Exception as e:
                    log.warning("unlatch failed: %s", e)
            threading.Thread(target=unlatch, daemon=True).start()

    def on_live_delegation(self, text: str) -> None:
        """Live mode: Codex handles the request; we only run the switch reflex on it."""
        if (target := parse_switch(text)) is not None:
            self._switch(target, None)

    def request_brain_switch(self, target: str) -> None:
        self.last_switch_t = -1e9
        self._switch(target, None)

    def _switch(self, target: str, item_id: str | None) -> None:
        if time.monotonic() - self.last_switch_t < 15:  # transcript and delegation of the same words
            return
        self.last_switch_t = time.monotonic()
        if target == "live":  # this service is live mode
            self._speak_or_append(item_id, "I'm already in live mode.")
            return
        log.info("switching brain to %s", target)
        self._speak_or_append(item_id, f"Switching to {SWITCH_LABEL[target]}.")
        asyncio.get_running_loop().call_later(3.0, switch.request_switch, target)  # let it say so first

    def on_request(self, text: str, item_id: str | None) -> None:
        """A request without the live brain (local fallback ASR, or GPT-Live in a non-live mode): only the reflexes."""
        log.info("heard (%s): %r", self.mode, text)
        if (target := parse_switch(text)) is not None:
            self._switch(target, item_id)
        elif STOP_WORDS.match(text):
            self._stop_body(text)
        else:
            self._speak_or_append(item_id, "My live brain is offline right now. Say: switch to local, for my "
                                           "local brain.")

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

    # ---- the voice session: wake word on, idle off ------------------------------------------------------------

    async def wake(self, why: str, hold: bool = True) -> bool:
        """Start the GPT-Live session (no-op if it is up). hold: replay the mic from just before the wake word into
        it, so "hey Tony, wave" arrives whole. True once the session hears the mic."""
        if self.live is None or self.mode != "gpt-live":
            return False
        if self.voice == "on" and self.live.voice_on:
            return True
        if hold:
            self.audio.start_hold()
        async with self._voice_lock:
            if self.voice == "on" and self.live.voice_on:
                self.audio.release_hold()
                return True
            self.voice = "starting"
            log.info("waking the voice (%s)", why)
            self.live.wake_reason = why
            try:
                await self.live.start_voice()
            except Exception as e:
                log.warning("GPT-Live session failed to start (%s): %s", why, e)
                self.voice = "off"
                self.audio.drop_hold()
                await self.live.stop_voice()
                if why.startswith("wake"):
                    await self.say_local("I can't reach my voice right now.")
                return False
            self.voice = "on"
            self.audio.release_hold()
            return True

    async def sleep_voice(self, why: str) -> None:
        async with self._voice_lock:
            if self.voice != "on":
                return
            self.voice = "off"
            log.info("voice going to sleep (%s); say 'Tony' to wake it", why)
            if self.spotter is not None:
                self.spotter.reset()
            await self.live.stop_voice()

    async def keep_voice(self) -> None:
        """Ends the session after VOICE_IDLE_S with nothing going on; brings it back if it dropped mid-work."""
        while True:
            await asyncio.sleep(1.0)
            live = self.live
            if live is None or self.voice == "starting":
                continue
            try:
                if self.voice == "on" and live.voice_closed.is_set():
                    self.voice = "off"
                    log.warning("GPT-Live session dropped")
                    await live.stop_voice()
                    if live.busy():
                        await self.wake("reconnect: the brain is still working", hold=False)
                elif (self.voice == "on" and VOICE_IDLE_S > 0 and self.spotter is not None and not live.busy() and not self.audio.speaking_local
                      and time.monotonic() - live.last_activity() > VOICE_IDLE_S):
                    await self.sleep_voice(f"idle {VOICE_IDLE_S:.0f} s")
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("keep_voice")

    def on_keyword(self, word: str) -> None:
        """The spotter heard a keyword while the voice slept or was starting (event loop)."""
        self.wakes += 1
        log.info("keyword %s", word)
        if word in ("STOP", "FREEZE", "HALT"):
            # A TV says "stop" too: the latched stop only while a move runs. Otherwise waking is enough (it ends an
            # autonomy session, which stops its body work without a latch), and GPT-Live hears the replayed "stop".
            loop = asyncio.get_running_loop()

            def stop_if_moving() -> None:
                try:
                    from tonypi.tonyd.client import TonydClient

                    c = TonydClient()
                    try:
                        busy = c.state().get("busy")
                    finally:
                        c.close()
                except Exception as e:
                    log.warning("tonyd state for %s: %s", word, e)
                    busy = True  # can't tell: stop
                if busy:
                    loop.call_soon_threadsafe(self._stop_body, word.lower())
                else:
                    log.info("keyword %s while nothing moves: waking only", word)
            threading.Thread(target=stop_if_moving, daemon=True).start()
        if self.voice == "off":
            if self.live is not None and hasattr(self.live, "_user_activity"):
                self.live._user_activity()  # the owner is here: an autonomy session ends
            asyncio.create_task(self.wake(f"wake word {word}"))

    async def start_spotter(self) -> None:
        self.spotter = await asyncio.to_thread(WakeSpotter)
        loop = asyncio.get_running_loop()
        buf = bytearray()
        q: "queue.Queue[np.ndarray]" = queue.Queue(maxsize=100)

        def feed(chunk: bytes) -> None:  # event loop: batch 0.1 s, spot in a thread
            if self.voice == "on" or self.mode != "gpt-live" or self.audio.speaking_local:
                buf.clear()
                return
            buf.extend(chunk)
            if len(buf) >= 3200:
                x = np.frombuffer(bytes(buf), np.int16).astype(np.float32) / 32768
                buf.clear()
                if not q.full():
                    q.put_nowait(x)

        def spot() -> None:
            while True:
                x = q.get()
                if self.voice == "on":  # GPT-Live hears everything now (its transcripts run the stop reflex)
                    continue
                if word := self.spotter.feed(x):
                    loop.call_soon_threadsafe(self.on_keyword, word)

        threading.Thread(target=spot, name="wake-spotter", daemon=True).start()
        self.audio.mic_listeners.append(feed)
        log.info("wake spotter ready ('Tony' starts the voice)")

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
        if VOICE_MODE == "live":
            from tonypi.voice.live import LiveBrain
        while self.live_enabled:
            self.live = LiveBrain(self) if VOICE_MODE == "live" else Live(self)
            helpers: list[asyncio.Task] = []
            try:
                await self.live.start_brain()
                self.mode = "gpt-live"
                if hasattr(self.live, "watchdog"):
                    helpers.append(asyncio.create_task(self.live.watchdog()))
                if hasattr(self.live, "start_autonomy") and (t := self.live.start_autonomy()) is not None:
                    helpers.append(t)
                helpers.append(asyncio.create_task(self.keep_voice()))
                if not await self.wake("service start", hold=False):  # awake at start (greets after a restart)
                    raise RuntimeError("the first GPT-Live session failed")
                await self.live.closed.wait()
                log.warning("the Codex app-server exited; restarting")
            except Exception as e:
                log.warning("GPT-Live unavailable (%s): local voice for now, retry in %d s", e, RETRY_LIVE_S)
            self.voice = "off"
            for t in helpers:
                t.cancel()
            if hasattr(self.live, "shutdown"):
                await self.live.shutdown()
            await self.live.close()
            self.live, self.mode = None, "local"
            await asyncio.sleep(RETRY_LIVE_S)

    # ---- socket -----------------------------------------------------------------------------------------------

    async def handle(self, msg: dict, writer: asyncio.StreamWriter) -> dict:
        op = msg.get("op")
        if op == "say":
            self._speak_or_append(None, str(msg.get("text") or ""))
            return {"spoken": True}
        if op == "inject":  # tests: play a WAV into the mic stream as if the user said it
            import wave

            with wave.open(str(msg["wav"])) as w:
                pcm = np.frombuffer(w.readframes(w.getnframes()), np.int16)
                if w.getnchannels() > 1:
                    pcm = pcm.reshape(-1, w.getnchannels())[:, 0]
                pcm = resample(pcm, w.getframerate(), MIC_RATE)
            self.audio.inject.extend(pcm.tobytes() + bytes(MIC_RATE))  # + 0.5 s of silence after it
            return {"seconds": round(len(pcm) / MIC_RATE, 2)}
        if op == "text":  # live mode: a typed request for the brain (turn, or steer of the running turn)
            if VOICE_MODE != "live" or self.live is None or not hasattr(self.live, "send_text"):
                raise ValueError("typed requests need live mode with the brain running")
            return await self.live.send_text(str(msg.get("text", "")), str(msg.get("source", "typed")))
        if op == "tasks":
            workers = getattr(self.live, "workers", {})
            return {"tasks": {k: {kk: vv for kk, vv in w.items() if kk not in ("started", "brief")}
                              for k, w in workers.items()}}
        if op == "status":
            auto = getattr(self.live, "autonomy", None)
            idle = time.monotonic() - self.live.last_activity() if self.live is not None and self.voice == "on" else None
            return {"mode": self.mode, "voice_mode": VOICE_MODE,
                    "voice": VOICE if self.mode == "gpt-live" else "piper",
                    "session": self.voice, "idle_s": None if idle is None else round(idle, 1),
                    "busy": bool(self.live is not None and self.live.busy()), "wakes": self.wakes,
                    "held_s": round(len(self.audio.hold or ()) * MIC_CHUNK / MIC_RATE, 1),
                    "mic_peaks": [int(np.abs(np.frombuffer(b, np.int16)).max(initial=0)) for b in
                                  list(self.audio.ring)[-50::5]],
                    "autonomy": auto.status() if auto is not None else None,
                    "spoken_s": {k: round(v, 1) for k, v in self.audio.spoken_s.items()},
                    "mic_s": round(self.audio.mic_chunks * MIC_CHUNK / MIC_RATE, 1),
                    "sent_to_live_s": round(self.audio.sent_chunks * MIC_CHUNK / MIC_RATE, 1)}
        if op == "ping":
            return {"pong": True}
        raise ValueError(f"unknown op {op!r}")

    async def client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
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

            asyncio.create_task(answer())  # a slow `text` (phone echo check) must not block other requests

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
        if self.live_enabled:
            try:
                await self.start_spotter()
            except Exception as e:
                log.warning("no wake spotter (%s): the voice session never sleeps", e)
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
