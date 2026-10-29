"""Listening flow with the real wake-word / streaming models and Piper speech, in simulated time.

After "hey Tony" Tony waits WAKE_START_S for the command to start; in the follow-up window FOLLOWUP_S. Once someone
talks, the command ends 0.7 s after they stop. Skipped when the speech models aren't installed.
"""

import threading
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("piper", reason="needs the voice extra: uv sync --all-extras")
pytest.importorskip("sherpa_onnx", reason="needs the voice extra: uv sync --all-extras")

from tonypi.agent import speech  # noqa: E402

pytestmark = pytest.mark.skipif(not (speech.ASR_DIR.exists() and speech.KWS_DIR.exists() and speech.VOICE.exists()),
                                reason="speech models not installed")


@pytest.fixture(scope="module")
def rig():
    from piper import PiperVoice, SynthesisConfig

    voice = PiperVoice.load(str(speech.VOICE))
    fixed = SynthesisConfig(noise_scale=0.0, noise_w_scale=0.0)  # same audio every run

    def synth(text):
        pcm = np.frombuffer(b"".join(c.audio_int16_bytes for c in voice.synthesize(text, fixed)), np.int16)
        pcm = pcm.astype(np.float32) / 32768
        idx = np.arange(0, len(pcm), voice.config.sample_rate / speech.RATE)
        return np.interp(idx, np.arange(len(pcm)), pcm).astype(np.float32)

    class Quiet:
        speaking, quiet_until = threading.Event(), 0.0

    events, clock = [], [0.0]
    lis = speech.Listener(Quiet(), lambda: events.append(("wake", clock[0])),
                          lambda text, audio=None: events.append((text, clock[0])), mode="stream")
    lis._cue = lambda kind: events.append((kind, clock[0]))
    lis.clock = lambda: clock[0]

    def play(*parts, followup=False):
        events.clear()
        clock[0] = 0.0
        st = lis.new_state()
        if followup:
            lis.followup()
        audio = np.concatenate([np.zeros(int(p * speech.RATE), np.float32) if isinstance(p, (int, float)) else p
                                for p in parts])
        for i in range(0, len(audio), 1600):
            lis.feed(st, audio[i:i + 1600])
            clock[0] += 0.1
        return [(e, round(t, 1)) for e, t in events]

    return synth, play


def commands(events):
    return [e for e, _ in events if e not in ("wake", "listen", "done")]


def waved(events):  # the streaming model's exact wording varies a little ("wave at us" / "wavered us")
    cmds = commands(events)
    return len(cmds) == 1 and cmds[0].startswith("wave") and cmds[0].endswith("us")


def test_wake_and_command_in_one_breath(rig):
    synth, play = rig
    assert waved(play(0.5, synth("Hey Tony, wave at us."), 2.0))


def test_pause_after_wake_phrase(rig):
    synth, play = rig
    assert waved(play(0.5, synth("Hey Tony."), 1.5, synth("Wave at us."), 2.0))


def test_wake_then_silence_gives_up_quickly(rig):
    synth, play = rig
    ev = play(0.5, synth("Hey Tony."), 6.0)
    assert commands(ev) == [""]
    wake_t = next(t for e, t in ev if e == "wake")
    assert next(t for e, t in ev if e == "") - wake_t < speech.WAKE_START_S + 1.5


def test_followup_window(rig):
    synth, play = rig
    assert waved(play(4.0, synth("Wave at us."), 2.0, followup=True))                     # no wake phrase
    ev = play(8.0, followup=True)                                                          # silence: closes quietly
    assert commands(ev) == [] and next(t for e, t in ev if e == "done") >= speech.FOLLOWUP_S
    assert commands(play(speech.FOLLOWUP_S + 1, synth("Wave at us."), 2.0, followup=True)) == []  # too late


def test_command_ends_soon_after_speech(rig):
    synth, play = rig
    cmd = synth("Hey Tony, wave at us.")
    ev = play(0.5, cmd, 2.0)
    spoken_end = 0.5 + len(cmd) / speech.RATE
    assert waved(ev) and next(t for e, t in ev if e.startswith("wave")) - spoken_end < 1.2
