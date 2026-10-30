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
  {"op":"record","seconds":5,"path":"/path.wav"}     debugging: record the mic (as GPT-Live hears it) to a WAV
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
# Leading fillers and punctuation are allowed: a replayed "Hey, Tony. Stop." or "Whoa, stop!" must match too.
STOP_WORDS = re.compile(r"^\W*(?:(?:hey|hi|ok|okay|oh|whoa|wait|no)\W+)*(?:tony\W+)?(stop|halt|freeze|stop it|stop moving)\b",
                        re.I)
STOP_ANY = re.compile(r"\b(stop|halt|freeze)\b", re.I)  # an utterance with one of these never lifts the stop latch
# The GPT-Live session runs only while someone talks to Tony: "Tony" / "hey Tony" (local keyword spotter) starts it, and
# it ends after this long with nothing going on (no user speech, no brain turn, no background task, Tony not talking).
# Tony's brain (the Codex app-server, thread L, tasks, autonomy) keeps running either way. 0 = never sleep.
VOICE_IDLE_S = float(os.environ.get("TONYPI_VOICE_IDLE_S", "30"))
PREROLL_CHUNKS = 100  # 2 s of mic kept before the wake word fired: GPT-Live hears "hey Tony, wave" whole
HOLD_MAX_CHUNKS = 1500  # 30 s: mic held while the session starts, then replayed into it
# Replay catches up with the live mic: sent REPLAY_SPEED x real time, and silences over 0.2 s skipped. A 20 ms chunk is
# silent below max(QUIET_RMS, QUIET_FACTOR x the held audio's noise floor (20th percentile RMS)): the mic runs at max
# gain, its floor (peaks ~1300) is far above digital silence (a fixed threshold skipped nothing: 22 s behind).
QUIET_RMS = 100.0
QUIET_FACTOR = 1.6
REPLAY_SPEED = float(os.environ.get("TONYPI_REPLAY_SPEED", "1.5"))
# A just-started session sometimes misses its first moments of audio even after "ready" (a 2x replay right at the
# start went unheard, 2026-10-30): the replay starts after this much real-time silence.
REPLAY_LEAD_CHUNKS = 25
VOICE_START_TIMEOUT_S = 25.0  # a session start that takes longer fails (below the 30 s hold: the command isn't evicted)
# Speech that never leads anywhere (a TV, people talking among themselves) keeps the session awake at most this long
# after the last real exchange (a delegation, a brain turn, Tony speaking, a typed request).
HEARD_AWAKE_S = float(os.environ.get("TONYPI_HEARD_AWAKE_S", "120"))
WAKE_FAILS_LOCAL = 2  # consecutive failed session starts -> the local ears (wake word + ASR) take requests to the brain

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
        self._lead = 0  # silent chunks still to send before the replay
        self.replay = {"held": 0, "sent": 0, "skipped": 0, "t0": 0.0}  # the last replay, for the log and status
        self.replay_done_t = 0.0  # when the last replay caught up with the live mic
        self.noise_rms = 300.0  # slow tracker of the mic's noise floor
        self.last_loud_t = 0.0  # last chunk clearly above the floor (someone may be talking)

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
                while True:  # a failed spawn (ENOMEM, EAGAIN) must not end the mic for good
                    await asyncio.sleep(0.5)
                    try:
                        await self._open_mic()
                        break
                    except OSError as e2:
                        log.warning("mic reopen failed: %s", e2)
                continue
            self.mic_chunks += 1
            if self.inject:
                buf, self.inject[:] = bytes(self.inject[:len(buf)]).ljust(len(buf), b"\0"), self.inject[len(buf):]
            elif time.monotonic() < self.mute_mic_until or (not DUPLEX and time.monotonic() < self.voice_until + ECHO_TAIL_S):
                buf = bytes(len(buf))
            for f in self.mic_listeners:
                try:
                    f(buf)
                except Exception:
                    log.exception("mic listener")
            self._track_level(buf)
            self.ring.append(buf)
            if self.hold is not None:
                self.hold.append(buf)
                if self.hold_released:
                    continue  # the session's mic track reads the hold until it has caught up
                buf = bytes(len(buf))  # the session isn't ready: what it would hear now is replayed later
            if self.mic_q.full():
                self.mic_q.get_nowait()
            self.mic_q.put_nowait(buf)

    def _track_level(self, buf: bytes) -> None:
        r = _rms(buf)
        if r <= 0:  # muted (Tony talking) or digital silence: says nothing about the room
            return
        self.noise_rms = 0.9 * self.noise_rms + 0.1 * r if r < self.noise_rms else 0.999 * self.noise_rms + 0.001 * r
        if r > 2.5 * max(self.noise_rms, QUIET_RMS):
            self.last_loud_t = time.monotonic()

    def start_hold(self) -> None:
        """The wake word fired: keep the last 2 s and everything from now on for the session that is starting.
        A hold of a session that has gone (released, never drained) is stale: start over."""
        if self.hold is None or self.hold_released:
            self.hold = collections.deque(self.ring, maxlen=HOLD_MAX_CHUNKS)
            self.hold_released, self._quiet_run = False, 0

    @property
    def replaying(self) -> bool:
        """Held audio not yet in the session (the user's words may be in it)."""
        return self.hold is not None

    def release_hold(self) -> None:
        """The session is ready: replay the held audio into it."""
        if self.hold is not None:
            self.hold_released = True
            self._lead = REPLAY_LEAD_CHUNKS
            if self.hold:
                floor = float(np.percentile([_rms(b) for b in self.hold], 20))
                self._quiet_rms = max(QUIET_RMS, QUIET_FACTOR * floor)
            self.replay = {"held": len(self.hold), "sent": 0, "skipped": 0, "t0": time.monotonic(),
                           "quiet_rms": round(self._quiet_rms)}
            while not self.mic_q.empty():  # silence queued while it started
                self.mic_q.get_nowait()
            # The mic track is usually already waiting in mic_q.get(), and _read_mic feeds the hold now, not mic_q:
            # without this one chunk that wait never ends and GPT-Live hears nothing (the 11:28 wake, 2026-10-30).
            self.mic_q.put_nowait(bytes(MIC_CHUNK * 2))

    def drop_hold(self) -> None:
        self.hold, self.hold_released = None, False

    async def next_chunk(self) -> bytes:
        """The next 20 ms for GPT-Live: held audio first (REPLAY_SPEED x, silences over 0.2 s skipped), then live."""
        while self.hold is not None and self.hold_released:
            if self._lead > 0:
                self._lead -= 1
                await asyncio.sleep(MIC_CHUNK / MIC_RATE)
                return bytes(MIC_CHUNK * 2)
            if not self.hold:
                r = self.replay
                log.info("replay caught up: %.1f s held, %.1f s sent, %.1f s silence skipped, in %.1f s (quiet < %s)",
                         r["held"] * MIC_CHUNK / MIC_RATE, r["sent"] * MIC_CHUNK / MIC_RATE,
                         r["skipped"] * MIC_CHUNK / MIC_RATE, time.monotonic() - r["t0"], r.get("quiet_rms"))
                self.replay_done_t = time.monotonic()
                self.drop_hold()
                break
            buf = self.hold.popleft()
            quiet = _rms(buf) < self._quiet_rms
            self._quiet_run = self._quiet_run + 1 if quiet else 0
            if self._quiet_run > 10:
                self.replay["skipped"] += 1
                continue
            self.replay["sent"] += 1
            await asyncio.sleep(MIC_CHUNK / MIC_RATE / REPLAY_SPEED)
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
        # the same measured echo tail as GPT-Live's voice: else the spotter hears the end of Tony's own sentence
        self.mute_mic_until = max(self.mute_mic_until, time.monotonic()) + len(pcm48) / OUT_RATE + ECHO_TAIL_S

    @property
    def speaking_local(self) -> bool:
        return len(self.local) > 0

    async def _mix(self) -> None:
        t = time.monotonic()
        play_failed = False
        high_since = None  # the speaker pipe has held > 0.5 s of audio since then
        while True:
            if time.monotonic() - t > 0.1:  # the loop stalled: don't burst the missed chunks out (re-anchor)
                t = time.monotonic()
            a, self.remote = self.remote[:OUT_CHUNK], self.remote[OUT_CHUNK:]
            b, self.local = self.local[:OUT_CHUNK], self.local[OUT_CHUNK:]
            out = np.zeros(OUT_CHUNK, np.int32)
            out[:len(a)] += a
            out[:len(b)] += b
            stalled = False
            try:  # aplay alive but not reading (USB stall): the pipe buffers forever and plays minutes late
                if self.play.stdin.transport.get_write_buffer_size() > OUT_RATE:  # > 0.5 s of audio
                    high_since = high_since or time.monotonic()
                    stalled = time.monotonic() - high_since > 1.5
                else:
                    high_since = None
            except Exception:
                pass
            if stalled:
                high_since = None
                log.warning("speaker stalled; restarting aplay")
                try:
                    self.play.kill()
                except ProcessLookupError:
                    pass
                play_failed = True
            if play_failed or self.play.returncode is not None or self.play.stdin.is_closing():
                # the speaker went away (USB hiccup, 2026-10-30: Tony was mute for an hour): reopen it. What was
                # queued is dropped: speech that couldn't play when it was current mustn't play minutes later (and
                # speaking_local would keep the spotter deaf meanwhile).
                log.warning("speaker stopped (aplay exit %s); reopening in 1 s", self.play.returncode)
                self.local = np.zeros(0, np.int16)
                self.remote = np.zeros(0, np.int16)
                await asyncio.sleep(1.0)
                try:
                    await self._open_play()
                    play_failed = False
                except OSError as e:
                    play_failed = True
                    log.warning("speaker reopen failed: %s", e)
                t = time.monotonic()
                continue
            try:
                self.play.stdin.write(np.clip(out, -32768, 32767).astype(np.int16).tobytes())
            except OSError as e:
                log.warning("speaker write failed: %s", e)
                play_failed = True
                continue
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
        self.spoke_t = 0.0  # last time something was handed to GPT-Live to say
        self.voice_gen = 0  # +1 per realtime session: ties delegations and bridged answers to their session
        self._starting = False  # a thread/realtime/start is in flight: closed/error events are an older session's
        self._stops_pending = 0  # thread/realtime/stop sent, its "closed (requested)" not seen yet
        self._codex_session_up = False  # Codex has a realtime session on the thread (even if our link died)
        self._mark_ready = lambda what: None
        self._pending: dict[int, asyncio.Future] = {}
        self._ids = itertools.count(1)

    async def _rpc(self, method: str, params: dict | None = None, timeout: float = 30) -> dict:
        if self.closed.is_set():
            raise ConnectionError("the codex app-server has exited")
        n = next(self._ids)
        fut = asyncio.get_running_loop().create_future()
        self._pending[n] = fut
        try:
            self.proc.stdin.write((json.dumps({"id": n, "method": method, "params": params or {}}) + "\n").encode())
            await self.proc.stdin.drain()
            msg = await asyncio.wait_for(fut, timeout)
        finally:  # timed out or cancelled: a late reply must find nothing (else the reader dies on it)
            self._pending.pop(n, None)
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
            try:
                await self._dispatch(m, sdp_q)
            except Exception:  # one bad message must not end the reader (the brain would go deaf for good)
                log.exception("app-server message %s", str(m)[:200])
        self.closed.set()
        self.voice_closed.set()
        for fut in self._pending.values():  # nothing will answer them: fail now, not after their timeouts
            if not fut.done():
                fut.set_exception(ConnectionError("the codex app-server has exited"))
        self._pending.clear()

    async def _dispatch(self, m: dict, sdp_q: asyncio.Queue) -> None:
        if "id" in m and "method" not in m:
            fut = self._pending.pop(m["id"], None)
            if fut is not None and not fut.done():
                fut.set_result(m)
            return
        meth = m.get("method", "")
        if "id" in m:  # a request from the app-server to us (tool call, approval, ...)
            asyncio.create_task(self._answer_server(m))
            return
        if meth == "thread/realtime/sdp":
            await sdp_q.put(m["params"]["sdp"])
        elif meth in ("thread/realtime/closed", "thread/realtime/error"):
            params = m.get("params") or {}
            requested = meth == "thread/realtime/closed" and params.get("reason") == "requested"
            # an older session's: during a start, or the answer to a stop we sent (it may come late)
            stale = self._starting or (requested and self._stops_pending > 0)
            if requested and self._stops_pending > 0:
                self._stops_pending -= 1
            (log.info if requested else log.warning)("%s %s%s", meth, json.dumps(params)[:300],
                                                     " (an older session's: ignored)" if stale else "")
            if not stale:
                self._codex_session_up = False
                self.voice_closed.set()
        else:
            if (meth == "thread/realtime/item/started"
                    and ((m.get("params") or {}).get("item") or {}).get("type") == "realtimeSessionStarted"):
                self._mark_ready("started")  # live mode's data channel has no session.started: Codex says so
            try:
                self.on_notification(meth, m.get("params") or {})
            except Exception:
                log.exception("notification %s", meth)

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
        """monotonic time of the last real exchange: the session started, Tony spoke or was given words to say
        (LiveBrain adds delegations, brain turns, typed requests)."""
        return max(self.voice_started_t, self.audio.voice_until, self.spoke_t)

    def heard_t(self) -> float:
        """monotonic time GPT-Live last heard speech (anyone's: the owner, a TV)."""
        return getattr(self.hub, "last_heard_t", 0.0)

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

        if self._codex_session_up:  # an older session may still be up on Codex: end it first
            await self._stop_session()
        await self._close_pc()
        self.ready = asyncio.Event()
        self.voice_closed = closed = asyncio.Event()
        self.voice_gen += 1
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

        self.pc = pc = RTCPeerConnection()
        self.pc.addTrack(Mic())
        self.dc = self.pc.createDataChannel("oai-events")
        rs = av.AudioResampler(format="s16", layout="mono", rate=OUT_RATE)

        @pc.on("connectionstatechange")
        def on_state():  # a dead link Codex never reports (ICE consent lost) would leave Tony deaf and awake
            if pc.connectionState in ("failed", "closed"):
                (log.warning if pc.connectionState == "failed" else log.info)("WebRTC connection %s", pc.connectionState)
                closed.set()

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
        t_start = time.monotonic()
        seen_t: dict[str, float] = {}

        def mark_ready(what: str) -> None:
            # audio sent before the session started AND media flows (data channel open) is lost (the first commands
            # after a start were never heard; a replayed "hey Tony, ..." too): ready 1 s after both
            seen.add(what)
            seen_t.setdefault(what, round(time.monotonic() - t_start, 1))
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
                closed.set()

        async def unless_closed(aw, what: str):
            """aw, but fail at once if the session closes meanwhile (Codex reports an error)."""
            task, stop = asyncio.ensure_future(aw), asyncio.ensure_future(closed.wait())
            try:
                await asyncio.wait({task, stop}, return_when=asyncio.FIRST_COMPLETED)
                if not task.done():
                    raise ConnectionError(f"the realtime session closed while waiting for {what}")
                return task.result()
            finally:  # also when cancelled (wake's timeout): a leftover _sdp_q.get() would eat the next answer
                stop.cancel()
                if not task.done():
                    task.cancel()

        t = [time.monotonic()]
        await self.pc.setLocalDescription(await self.pc.createOffer())
        t.append(time.monotonic())
        params = self.realtime_params(self.pc.localDescription.sdp)
        t.append(time.monotonic())
        self._starting = True
        try:
            await self._rpc("thread/realtime/start", params)
            self._codex_session_up = True
        finally:
            self._starting = False
        t.append(time.monotonic())
        answer = await unless_closed(self._sdp_q.get(), "the SDP answer")
        t.append(time.monotonic())
        await self.pc.setRemoteDescription(RTCSessionDescription(answer, "answer"))
        await unless_closed(self.ready.wait(), "the session to start")
        self.voice_started_t = time.monotonic()
        t.append(self.voice_started_t)
        log.info("GPT-Live session up in %.1f s (offer/params/start/answer/ready %s; %s; voice %s)", t[-1] - t[0],
                 " ".join(f"{b - a:.1f}" for a, b in zip(t, t[1:])), seen_t, VOICE)

    def _send(self, ev: dict) -> bool:
        # only into the current session: one going to sleep (hub.voice already "off") would drop it unheard
        if self.dc is None or self.dc.readyState != "open" or getattr(self.hub, "voice", "on") != "on":
            return False
        self.dc.send(json.dumps(ev))
        if ev.get("channel") == "speakable":
            self.spoke_t = time.monotonic()  # GPT-Live starts talking ~1 s later: don't sleep in between
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

    async def _stop_session(self) -> None:
        # also when only our side saw the link die (voice_closed set locally): Codex may still hold the session
        if self.tid and self.proc and self.proc.returncode is None and self._codex_session_up:
            self._codex_session_up = False
            self._stops_pending += 1
            try:
                await self._rpc("thread/realtime/stop", {"threadId": self.tid}, timeout=5)
            except Exception as e:
                log.warning("thread/realtime/stop: %s", e)
        self.voice_closed.set()

    async def stop_voice(self) -> None:
        """End the realtime session; the thread and the app-server stay."""
        # What wasn't replayed is gone with the session (else the next wake replays it). Dropped before the RPC: a
        # wake word heard while it runs starts a new hold, which must survive for the next session.
        self.audio.drop_hold()
        await self._stop_session()
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
        self.pending_speech: list[str] = []  # handed over while a session was starting: spoken once it is up
        self.wake_failures = 0  # consecutive failed session starts
        self.next_retry_t = 0.0  # keep_voice: earliest next reconnect / retry
        self.spot_stop_t = -1e9  # the spotter last heard stop / freeze / halt
        self.last_wake_t = -1e9  # the spotter last heard the wake word
        self.reconnect = False  # the session dropped while the brain worked: bring it back (not after a sleep)
        self.local_fallback = False  # GPT-Live keeps failing: the local ears stand in (announced once)

    # ---- reflexes ------------------------------------------------------------------------------------------

    def on_transcript(self, text: str) -> None:
        """What GPT-Live heard, piece by piece: the stop reflex, and the whole utterance once it pauses (switch
        commands may never become a delegation: GPT-Live may find a misheard brain name meaningless)."""
        if STOP_WORDS.match("".join(self.utt_parts) + text):
            if self.audio.replaying and time.monotonic() - self.spot_stop_t < 30:
                log.info("stop in replayed audio: the spotter already stopped tonyd")  # (and its interrupt_all
            else:                                                                     # would kill the next request)
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

                c = TonydClient()
                try:
                    c.stop(latch=VOICE_MODE == "live")  # live: no brain may carry on moving
                finally:
                    c.close()
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
            self._unlatch(text)

    def _unlatch(self, text: str) -> None:
        """The user spoke again after a stop: moving is allowed again. Not when the words hold a stop word ("Hey,
        Tony. Stop. ..."), nor when they are the replay of what the spotter's stop was heard in."""
        now = time.monotonic()
        if STOP_ANY.search(text):
            log.info("no unlatch: %r has a stop word", text)
            return
        if now - self.spot_stop_t < 30 and (self.audio.replaying or now - self.audio.replay_done_t < 3):
            log.info("no unlatch: %r is replayed audio from around a stop", text)
            return

        def unlatch() -> None:
            try:
                from tonypi.tonyd.client import TonydClient

                c = TonydClient()
                try:
                    c.unlatch()
                finally:
                    c.close()
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
        elif (VOICE_MODE == "live" and self.live is not None and hasattr(self.live, "send_text")
              and not self.live.closed.is_set() and text.strip()):
            asyncio.create_task(self._local_request(text))  # GPT-Live is down but the brain is up
        else:
            self._speak_or_append(item_id, "My live brain is offline right now. Say: switch to local, for my "
                                           "local brain.")

    async def _local_request(self, text: str) -> None:
        self._unlatch(text)
        try:
            await self.live.send_text(text, "local_asr")
        except Exception as e:
            log.warning("local request %r failed: %s", text, e)
            await self.say_local("Sorry, my brain didn't get that.")

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
        live = self.live
        if live is None or self.mode != "gpt-live":
            return False
        if self.voice == "on" and live.voice_on:
            return True
        if hold:
            self.audio.start_hold()
        async with self._voice_lock:
            if self.live is not live or self.mode != "gpt-live":  # the brain went away while we waited
                self.audio.drop_hold()
                return False
            if self.voice == "on" and live.voice_on:
                self.audio.release_hold()
                return True
            self.voice = "starting"
            log.info("waking the voice (%s)", why)
            live.wake_reason = why
            t0 = time.monotonic()
            try:
                await asyncio.wait_for(live.start_voice(), VOICE_START_TIMEOUT_S)
            except Exception as e:
                log.warning("GPT-Live session failed to start (%s): %s", why, e or type(e).__name__)
                self.voice = "off"
                await live.stop_voice()  # drops the hold too
                self.wake_failures += 1
                self.next_retry_t = time.monotonic() + min(60.0, 5.0 * 2 ** (self.wake_failures - 1))
                speech, self.pending_speech = self.pending_speech, []
                for text in speech:
                    await self.say_local(text)
                can_local = VOICE_MODE == "live" and hasattr(live, "send_text")
                if self.wake_failures >= WAKE_FAILS_LOCAL and can_local:
                    await self._go_local()
                elif why.startswith("wake") or self.last_wake_t >= t0:  # someone called Tony: say why nothing comes
                    await self.say_local("I can't reach my voice right now.")
                return False
            self.voice = "on"
            self.wake_failures = 0
            if self.local_fallback:
                self.local_fallback = False
                log.info("GPT-Live is back: the local ears stand down")
            replaying = self.audio.hold is not None
            self.audio.release_hold()
            speech, self.pending_speech = self.pending_speech, []
            if speech:
                asyncio.create_task(self._flush_speech(live, speech, replaying))
            return True

    async def _flush_speech(self, live: Live, speech: list[str], after_replay: bool) -> None:
        """Say what waited for the session: after the held words are replayed (else GPT-Live hears them as the
        owner talking over it and cuts itself off), at most 15 s later."""
        if after_replay:
            for _ in range(150):
                if not self.audio.replaying:
                    break
                await asyncio.sleep(0.1)
        for text in speech:
            if self.live is not live or not live.speak(text):
                await self.say_local(text)

    async def _go_local(self) -> None:
        """GPT-Live keeps failing: the local ears (wake word + ASR, Piper) take requests to the brain, which stays
        up; keep_voice tries GPT-Live again every RETRY_LIVE_S."""
        self.mode = "local"
        self.next_retry_t = time.monotonic() + RETRY_LIVE_S
        if self.local_fallback:  # a retry failed again: no new announcement
            return
        self.local_fallback = True
        log.warning("GPT-Live failed %d times: local ears until it comes back", self.wake_failures)
        await self.say_local("My voice connection is down. I'm listening with my own ears: say hey Tony, then "
                             "your request.")

    async def sleep_voice(self, why: str) -> None:
        async with self._voice_lock:
            if self.voice != "on":
                return
            self.voice = "off"
            log.info("voice going to sleep (%s); say 'Tony' to wake it", why)
            if self.spotter is not None:
                self.spotter.reset()
            await self.live.stop_voice()

    def voice_idle_s(self) -> float:
        """Seconds without a real exchange; speech that leads nowhere counts for at most HEARD_AWAKE_S."""
        live = self.live
        core = live.last_activity()
        heard = max(live.heard_t(), self.audio.last_loud_t)
        return time.monotonic() - max(core, min(heard, core + HEARD_AWAKE_S))

    async def keep_voice(self) -> None:
        """Ends the session after VOICE_IDLE_S with nothing going on; brings it back while the brain works (with
        backoff); retries GPT-Live while the local ears stand in."""
        while True:
            await asyncio.sleep(1.0)
            live = self.live
            if live is None or self.voice == "starting":
                continue
            try:
                now = time.monotonic()
                if self.mode == "local":
                    if now >= self.next_retry_t and not live.closed.is_set():
                        self.next_retry_t = now + RETRY_LIVE_S
                        self.mode = "gpt-live"
                        if await self.wake("retry after the local fallback", hold=False):
                            log.info("GPT-Live is back")
                        else:
                            self.mode = "local"
                    continue
                if self.voice == "on" and live.voice_closed.is_set():
                    self.voice = "off"
                    log.warning("GPT-Live session dropped")
                    await live.stop_voice()
                    self.next_retry_t, self.reconnect = 0.0, True
                if self.reconnect and (self.voice == "on" or not live.busy()):
                    self.reconnect = False  # back, or nothing left to say
                if self.voice == "off" and self.reconnect and now >= self.next_retry_t:
                    await self.wake("reconnect: the brain is still working", hold=False)
                elif (self.voice == "on" and VOICE_IDLE_S > 0 and self.spotter is not None and not live.busy()
                      and not self.audio.speaking_local and not self.audio.replaying
                      and self.voice_idle_s() > VOICE_IDLE_S):
                    await self.sleep_voice(f"idle {VOICE_IDLE_S:.0f} s")
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("keep_voice")

    def on_keyword(self, word: str) -> None:
        """The spotter heard a keyword while the voice slept, was starting, or replayed held audio (event loop)."""
        self.wakes += 1
        log.info("keyword %s (voice %s)", word, self.voice)
        live = self.live
        if word in ("STOP", "FREEZE", "HALT"):
            self.spot_stop_t = time.monotonic()
            # A TV says "stop" too: stopped (latched) only while Tony moves or body work is going on (between two
            # trial moves tonyd isn't busy); else ignored. Never replayed: the replay would latch it anyway.
            body_work = bool(live is not None and hasattr(live, "body_work_active") and live.body_work_active())
            loop = asyncio.get_running_loop()

            def stop_if_moving() -> None:
                busy = body_work
                if not busy:
                    try:
                        from tonypi.tonyd.client import TonydClient

                        c = TonydClient()
                        try:
                            busy = bool(c.state().get("busy"))
                        finally:
                            c.close()
                    except Exception as e:
                        log.warning("tonyd state for %s: %s", word, e)
                        busy = True  # can't tell: stop
                loop.call_soon_threadsafe(self._spotted_stop, word, busy)
            threading.Thread(target=stop_if_moving, daemon=True).start()
            return
        if self.voice == "on":  # replaying: GPT-Live has these words already
            return
        self.last_wake_t = time.monotonic()
        if live is not None and hasattr(live, "_user_activity"):
            live._user_activity()  # the owner is here: an autonomy session ends
        if self.voice == "starting":
            self.audio.start_hold()  # the start in progress replays it (a typed request or reconnect had none)
        else:
            asyncio.create_task(self.wake(f"wake word {word}"))

    def _spotted_stop(self, word: str, moving: bool) -> None:
        if not moving:
            log.info("keyword %s while nothing moves: ignored", word)
            return
        self._stop_body(word.lower())
        if self.voice == "off":  # the owner is here and may say more: wake (the replay holds the stop: latched anyway)
            asyncio.create_task(self.wake(f"wake word {word}"))

    async def start_spotter(self) -> None:
        self.spotter = await asyncio.to_thread(WakeSpotter)
        loop = asyncio.get_running_loop()
        buf = bytearray()
        q: "queue.Queue[np.ndarray]" = queue.Queue(maxsize=100)

        def deaf() -> bool:
            """GPT-Live hears the live mic itself (its transcripts run the stop reflex): no spotting. While it is
            still catching up on held audio, the spotter keeps listening for stop."""
            return self.voice == "on" and not self.audio.replaying

        def feed(chunk: bytes) -> None:  # event loop: batch 0.1 s, spot in a thread
            if deaf() or self.mode != "gpt-live" or self.audio.speaking_local:
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
                if deaf():
                    continue
                try:
                    word = self.spotter.feed(x)
                except Exception:
                    log.exception("wake spotter")
                    continue
                if word:
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
                local = self.mode == "local" or (self.local_fallback and self.voice != "on")
                if local and not self.audio.speaking_local and not q.full():
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
                    log.warning("the first GPT-Live session failed: the brain stays up; a wake word or retry brings it")
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
        if op == "record":  # debugging: the next N s of the mic as GPT-Live / the spotter get it, to a WAV
            import wave

            seconds, path = min(float(msg.get("seconds", 5)), 60.0), str(msg["path"])
            got: list[bytes] = []
            self.audio.mic_listeners.append(got.append)
            try:
                await asyncio.sleep(seconds)
            finally:
                self.audio.mic_listeners.remove(got.append)
            pcm = np.frombuffer(b"".join(got), np.int16)
            with wave.open(path, "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(MIC_RATE)
                w.writeframes(pcm.tobytes())
            return {"seconds": round(len(pcm) / MIC_RATE, 2), "path": path,
                    "rms": round(float(np.sqrt(np.mean(pcm.astype(np.float32) ** 2))) if len(pcm) else 0.0, 1)}
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
            idle = self.voice_idle_s() if self.live is not None and self.voice == "on" else None
            return {"mode": self.mode, "voice_mode": VOICE_MODE,
                    "voice": VOICE if self.mode == "gpt-live" else "piper",
                    "session": self.voice, "idle_s": None if idle is None else round(idle, 1),
                    "busy": bool(self.live is not None and self.live.busy()), "wakes": self.wakes,
                    "held_s": round(len(self.audio.hold or ()) * MIC_CHUNK / MIC_RATE, 1),
                    "replay": {k: (round(v, 1) if isinstance(v, float) else v) for k, v in self.audio.replay.items()},
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
