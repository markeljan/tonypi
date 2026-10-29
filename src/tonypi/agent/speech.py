"""Speaker (Piper TTS → USB speaker) and Listener (mic → "hey Tony" keyword spotter → streaming speech-to-text)."""

from __future__ import annotations

import base64
import collections
import io
import json
import logging
import math
import os
import re
import struct
import subprocess
import tempfile
import threading
import time
import urllib.request
import wave
from pathlib import Path
from typing import Callable

import numpy as np

log = logging.getLogger(__name__)

MODELS = Path("/home/pi/models")
VOICE = MODELS / "en_US-lessac-medium.onnx"
KWS_DIR = MODELS / "sherpa-onnx-kws-zipformer-gigaspeech-3.3M-2024-01-01"
ASR_DIR = MODELS / "sherpa-onnx-streaming-zipformer-en-2023-06-26"
ALSA_DEV = "plughw:0,0"  # USB "PnP Audio Device": speaker + mic
RATE = 16000
WAKE_PHRASES = ["HEY TONY", "HI TONY", "HEY TONI", "OK TONY", "A TONY"]
PREROLL_S = 2.5  # the spotter fires ~0.3-1 s after "Tony", so the transcriber gets the recent audio too
# how the transcriber writes the wake phrase: "hey tony", "haytony", "a toni", "eight only", ...
_TONY = re.compile(r"\w*(t|th)(ony|oni|onie|oney|onya|iny|one)|\w*(only|pony|tommy)", re.I)
_HEY = re.compile(r"(hey|hay|hi|a|eight|ok|okay|hello)", re.I)
# biases the transcriber toward Tony's vocabulary
ASR_URL = os.environ.get("TONYPI_ASR_URL", "http://127.0.0.1:8080/v1/chat/completions")  # LFM3-Omni; "" = streaming only
# in an LFM3 transcript of the pre-roll + command: everything up to the wake phrase ("..., hey Tony, dance")
_WAKE_TEXT = re.compile(r"^.*?\b(?:(?:hey|hi|hay|okay|ok|a)[\s,]+)?(?:tony|toni|tonie|toney)\b[\s,.!?]*", re.I | re.S)
HOTWORDS = ["TONY", "KUNG FU", "DANCE", "WAVE", "BOW", "SQUAT", "TWIST", "NOD", "WALK FORWARD", "WALK BACKWARD",
            "TURN LEFT", "TURN RIGHT", "LIE DOWN", "GET UP", "WHAT DO YOU SEE", "STOP"]


def command_from_words(words: list[tuple[str, float]], wake_t: float | None) -> str:
    """Drop the wake phrase (and any chatter before it) from a transcript of (word, start time) pairs.

    wake_t is when the spotter fired, relative to the transcript start (None: no pre-roll in this transcript).
    The wake phrase is the last "Tony"-like word that started before wake_t; the command is what follows it.
    """
    if wake_t is not None:
        before = [i for i, (w, t) in enumerate(words) if t < wake_t and _TONY.fullmatch(w)]
        if before:
            words = words[before[-1] + 1:]
        else:  # transcriber mangled "Tony": drop what's clearly before, then a leading "hey"
            words = [(w, t) for w, t in words if t >= wake_t - 1.0]
            while words and (_HEY.fullmatch(words[0][0]) or _TONY.fullmatch(words[0][0])) and words[0][1] < wake_t:
                words = words[1:]
    return " ".join(w for w, _ in words).lower().strip()


def lfm_transcribe(audio: np.ndarray, fallback: str, has_preroll: bool) -> str:
    """Re-transcribe a finished command with LFM3 (0% vs 18% word errors on the stock mic; ~2.5 s for 3 s).

    The streaming zipformer still does wake word timing and end-of-speech; its text is the fallback."""
    if not ASR_URL or len(audio) < RATE // 4:
        return fallback
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1), w.setsampwidth(2), w.setframerate(RATE)
        w.writeframes((np.clip(audio, -1, 1) * 32767).astype(np.int16).tobytes())
    body = {"messages": [{"role": "system", "content": "Perform ASR in English."},
                         {"role": "user", "content": [{"type": "input_audio", "input_audio": {
                             "data": base64.b64encode(buf.getvalue()).decode(), "format": "wav"}}]}],
            "max_tokens": 80, "temperature": 0, "cache_prompt": False,
            "chat_template_kwargs": {"enable_thinking": False}}
    try:
        req = urllib.request.Request(ASR_URL, json.dumps(body).encode(), {"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            text = (json.load(r)["choices"][0]["message"].get("content") or "").strip()
    except Exception as e:
        log.warning("LFM3 ASR failed (%s); using streaming transcript", e)
        return fallback
    if has_preroll:
        m = _WAKE_TEXT.match(text)
        if not m:  # can't tell where the command starts
            log.info("LFM3 transcript without wake phrase %r; using streaming transcript", text)
            return fallback
        text = text[m.end():]
    text = text.strip().rstrip(".")
    log.info("transcripts: streaming=%r lfm3=%r", fallback, text)
    return text or fallback


class Speaker:
    def __init__(self) -> None:
        from piper import PiperVoice

        self.voice = PiperVoice.load(str(VOICE))
        self.rate = self.voice.config.sample_rate
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self.speaking = threading.Event()
        self.quiet_until = 0.0  # the listener ignores the mic until then (no hearing ourselves)

    def say(self, text: str) -> None:
        """Blocking. Interruptible with stop()."""
        text = text.strip()
        if not text:
            return
        pcm = b"".join(c.audio_int16_bytes for c in self.voice.synthesize(text))
        self._play(pcm, self.rate)

    def chirp(self) -> None:
        """Short rising two-tone: 'I'm listening'."""
        pcm = b""
        for f in (880, 1320):
            n = int(0.07 * RATE)
            pcm += b"".join(struct.pack("<h", int(9000 * math.sin(2 * math.pi * f * i / RATE) * min(1, (n - i) / 300)))
                            for i in range(n))
        self._play(pcm, RATE)

    def stop(self) -> None:
        with self._lock:
            if self._proc and self._proc.poll() is None:
                self._proc.kill()

    def _play(self, pcm: bytes, rate: int) -> None:
        self.speaking.set()
        try:
            with self._lock:
                self._proc = subprocess.Popen(
                    ["aplay", "-q", "-D", ALSA_DEV, "-t", "raw", "-f", "S16_LE", "-c", "1", "-r", str(rate)],
                    stdin=subprocess.PIPE)
            try:
                self._proc.stdin.write(pcm)
                self._proc.stdin.close()
            except BrokenPipeError:
                pass
            self._proc.wait()
        finally:
            self.quiet_until = time.monotonic() + 0.4
            self.speaking.clear()


def _tmp(lines) -> str:
    f = tempfile.NamedTemporaryFile("w", prefix="tonypi-", suffix=".txt", delete=False)
    f.writelines(line + "\n" for line in lines)
    f.close()
    return f.name


def _keywords_file(bpe_model: Path, phrases: list[str]) -> str:
    """Wake phrases → sherpa-onnx keyword file of BPE pieces."""
    import sentencepiece as spm

    sp = spm.SentencePieceProcessor(model_file=str(bpe_model))
    return _tmp(" ".join(sp.encode(p, out_type=str)) + f" @{p.replace(' ', '_')}" for p in phrases)


def _bpe_vocab(bpe_model: Path) -> str:
    """bpe.model → 'piece<TAB>score' vocab that sherpa-onnx needs to encode plain-text hotwords."""
    import sentencepiece as spm

    sp = spm.SentencePieceProcessor(model_file=str(bpe_model))
    return _tmp(f"{sp.id_to_piece(i)}\t{sp.get_score(i)}" for i in range(sp.get_piece_size()))


class Listener:
    """Always-on: waits for the wake phrase, then transcribes until the speaker pauses.

    on_wake() is called when the wake phrase is heard, on_command(text) with the transcript ("" if nothing heard).
    """

    def __init__(self, speaker: Speaker, on_wake: Callable[[], None], on_command: Callable[[str], None],
                 kws_threshold: float = 0.1, kws_score: float = 2.5,
                 transcribe: Callable[[np.ndarray, str, bool], str] | None = lfm_transcribe) -> None:
        import sherpa_onnx

        self.speaker = speaker
        self.on_wake = on_wake
        self.on_command = on_command
        self.transcribe = transcribe
        self.kws = sherpa_onnx.KeywordSpotter(
            tokens=str(KWS_DIR / "tokens.txt"),
            encoder=str(KWS_DIR / "encoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx"),
            decoder=str(KWS_DIR / "decoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx"),
            joiner=str(KWS_DIR / "joiner-epoch-12-avg-2-chunk-16-left-64.int8.onnx"),
            keywords_file=_keywords_file(KWS_DIR / "bpe.model", WAKE_PHRASES), num_threads=1,
            keywords_score=kws_score, keywords_threshold=kws_threshold)
        self.asr = sherpa_onnx.OnlineRecognizer.from_transducer(
            tokens=str(ASR_DIR / "tokens.txt"),
            encoder=str(ASR_DIR / "encoder-epoch-99-avg-1-chunk-16-left-128.int8.onnx"),
            decoder=str(ASR_DIR / "decoder-epoch-99-avg-1-chunk-16-left-128.int8.onnx"),
            joiner=str(ASR_DIR / "joiner-epoch-99-avg-1-chunk-16-left-128.int8.onnx"),
            num_threads=2, sample_rate=RATE, enable_endpoint_detection=True,
            decoding_method="modified_beam_search", hotwords_score=1.5,
            hotwords_file=_tmp(HOTWORDS), modeling_unit="bpe", bpe_vocab=_bpe_vocab(ASR_DIR / "bpe.model"),
            rule1_min_trailing_silence=3.0,   # nothing said after the wake phrase
            rule2_min_trailing_silence=0.9,   # pause after speaking = end of command
            rule3_min_utterance_length=15.0)
        self._running = False
        self._thread: threading.Thread | None = None
        self._proc: subprocess.Popen | None = None

    def start(self) -> None:
        self._running = True
        self._thread = threading.Thread(target=self._loop, name="listener", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._proc:
            self._proc.kill()

    # feed() is separate from the mic loop so it can be tested with recorded/synthesised audio
    def new_state(self) -> dict:
        return {"kws": self.kws.create_stream(), "asr": None, "t_wake": 0.0, "wake_t": None, "retry": False,
                "ring": collections.deque(maxlen=int(PREROLL_S * 10) + 1), "audio": []}

    def feed(self, st: dict, samples: np.ndarray) -> None:
        if st["asr"] is None:
            st["ring"].append(samples)
            st["kws"].accept_waveform(RATE, samples)
            while self.kws.is_ready(st["kws"]):
                self.kws.decode_stream(st["kws"])
                if self.kws.get_result(st["kws"]):
                    self.kws.reset_stream(st["kws"])
                    pre = np.concatenate(st["ring"])
                    st["ring"].clear()
                    st["asr"] = self.asr.create_stream()
                    st["asr"].accept_waveform(RATE, pre)
                    st["audio"] = [pre]
                    st["wake_t"] = len(pre) / RATE  # detection time within this transcript
                    st["retry"] = False
                    st["t_wake"] = time.monotonic()
                    log.info("wake phrase heard")
                    self.on_wake()
                    return
        else:
            s = st["asr"]
            s.accept_waveform(RATE, samples)
            st["audio"].append(samples)
            while self.asr.is_ready(s):
                self.asr.decode_stream(s)
            timed_out = time.monotonic() - st["t_wake"] > 15
            if self.asr.is_endpoint(s) or timed_out:
                text = self._command_text(s, st["wake_t"])
                if not text and not st["retry"] and not timed_out:
                    # "Hey Tony ... <pause> ... dance": the pause ended the first segment; listen once more
                    st["asr"], st["wake_t"], st["retry"], st["audio"] = self.asr.create_stream(), None, True, []
                    return
                self._done(st, text)

    def finish(self, st: dict) -> None:
        """End of input (tests): flush a pending command."""
        if st["asr"] is not None:
            st["asr"].input_finished()
            while self.asr.is_ready(st["asr"]):
                self.asr.decode_stream(st["asr"])
            self._done(st, self._command_text(st["asr"], st["wake_t"]))

    def _done(self, st: dict, text: str) -> None:
        audio, has_preroll = np.concatenate(st["audio"] or [np.zeros(0, np.float32)]), st["wake_t"] is not None
        st["asr"], st["audio"] = None, []
        st["kws"] = self.kws.create_stream()
        if self.transcribe is None:
            log.info("heard command: %r", text)
            self.on_command(text)
            return

        def second_pass() -> None:  # off the mic thread, so audio keeps flowing
            final = self.transcribe(audio, text, has_preroll)
            log.info("heard command: %r", final)
            self.on_command(final)

        threading.Thread(target=second_pass, name="asr-2nd-pass", daemon=True).start()

    def _command_text(self, s, wake_t: float | None) -> str:
        r = self.asr.get_result_all(s)
        words: list[list] = []  # [word, start time]
        for tok, t in zip(r.tokens, r.timestamps):
            if tok.startswith(" ") or not words:
                words.append([tok.strip(), t])
            else:
                words[-1][0] += tok
        log.info("transcript %s (wake at %s)", [(w, round(t, 2)) for w, t in words],
                 None if wake_t is None else round(wake_t, 2))
        return command_from_words([(w, t) for w, t in words], wake_t)

    def _loop(self) -> None:
        chunk = int(0.1 * RATE)
        while self._running:
            self._proc = subprocess.Popen(
                ["arecord", "-q", "-D", ALSA_DEV, "-f", "S16_LE", "-r", str(RATE), "-c", "1", "-t", "raw"],
                stdout=subprocess.PIPE)
            st = self.new_state()
            try:
                while self._running:
                    buf = self._proc.stdout.read(chunk * 2)
                    if not buf:
                        break
                    if self.speaker.speaking.is_set() or time.monotonic() < self.speaker.quiet_until:
                        if st["asr"] is None:
                            continue  # ignore ourselves; but don't cut off a command being spoken
                    samples = np.frombuffer(buf, dtype=np.int16).astype(np.float32) / 32768.0
                    self.feed(st, samples)
            except Exception:
                log.exception("listener error")
            finally:
                self._proc.kill()
            if self._running:
                log.warning("mic stream ended; restarting in 2 s")
                time.sleep(2)
