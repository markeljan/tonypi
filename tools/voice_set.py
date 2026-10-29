"""Build the voice test set used by tools/voice_eval.py.

usage: uv run python tools/voice_set.py OUT_DIR [REAL_RECORDING.wav]
Writes 16 kHz mono WAVs + manifest.json:
  cmd_*.wav     "Hey Tony, <command>" for every brain_eval command (Piper voice), with expected actions
  real_*.wav    the six commands cut from a real recording of tools/voice_set.REAL_SCRIPT (if given)
  addr_*.wav    addressee set: lines spoken TO the robot vs room chatter (some mention Tony, one sounds like it)
"""

import json
import sys
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from brain_eval import CASES  # noqa: E402

RATE = 16000
REAL_SCRIPT = [  # what the user read, in order, with the expected actions
    ("Hey Tony, show me some kung fu.", [["kung_fu"]], None),
    ("Can you walk forward three steps and then turn left?", [["walk_forward", "turn_left"]], None),
    ("What's the capital of Japan?", [[]], r"tokyo"),
    ("Flap your wings like a bird.", [["flap_wings"]], None),
    ("How's your battery doing?", [["report_status"]], None),
    ("Go to sleep for a while.", [["sleep"]], None),
]
TO_ROBOT = ["Hey Tony, wave at us.", "Tony, can you dance for me?", "Hey Tony, what do you see right now?",
            "Tony, stop.", "Hey robot, take a bow.", "Tony, how is your battery?", "Hi Tony, nod your head.",
            "Okay Tony, look to the left."]
CHATTER = ["I told Tony about the movie yesterday.", "Did you remember to feed the dog?", "Pass me the remote, please.",
           "We should go dancing this weekend.", "The weather is supposed to be nice tomorrow.",
           "Tony from work called while you were out.", "Hey honey, can you grab the milk?",
           "And in other news, the stock market rose two percent today.", "I think the robot is cute.",
           "Can you believe what happened at the game last night?"]


def synth(voice, text: str) -> np.ndarray:
    pcm = np.frombuffer(b"".join(c.audio_int16_bytes for c in voice.synthesize(text)), np.int16).astype(np.float32)
    sr = voice.config.sample_rate
    out = np.interp(np.arange(0, len(pcm), sr / RATE), np.arange(len(pcm)), pcm)
    return np.concatenate([np.zeros(RATE // 4), out, np.zeros(RATE // 4)]).astype(np.int16)


def write(path: Path, pcm: np.ndarray) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1), w.setsampwidth(2), w.setframerate(RATE)
        w.writeframes(pcm.astype(np.int16).tobytes())


def split_real(pcm: np.ndarray, n: int) -> list[np.ndarray]:
    """Cut a recording into its n utterances at pauses longer than 0.8 s."""
    fr = pcm[: len(pcm) // 160 * 160].reshape(-1, 160).astype(float)
    on = np.where(np.sqrt((fr ** 2).mean(1)) > 800)[0]
    segs, s0, prev = [], on[0], on[0]
    for i in on[1:]:
        if i - prev > 80:
            segs.append((s0, prev))
            s0 = i
        prev = i
    segs.append((s0, prev))
    if len(segs) != n:
        raise SystemExit(f"found {len(segs)} utterances, expected {n}")
    return [pcm[max(0, a - 20) * 160:(b + 30) * 160] for a, b in segs]


def main() -> None:
    from piper import PiperVoice

    out = Path(sys.argv[1])
    out.mkdir(parents=True, exist_ok=True)
    voice = PiperVoice.load("/home/pi/models/en_US-lessac-medium.onnx")
    manifest = []
    for i, (q, accept, say_re, counts) in enumerate(CASES):
        f = f"cmd_{i:02d}.wav"
        write(out / f, synth(voice, f"Hey Tony, {q}"))
        manifest.append({"file": f, "kind": "cmd", "text": q, "accept": accept, "say_re": say_re, "counts": counts})
    if len(sys.argv) > 2:
        with wave.open(sys.argv[2]) as w:
            real = np.frombuffer(w.readframes(w.getnframes()), np.int16)
        for i, (clip, (text, accept, say_re)) in enumerate(zip(split_real(real, len(REAL_SCRIPT)), REAL_SCRIPT)):
            f = f"real_{i}.wav"
            write(out / f, clip)
            manifest.append({"file": f, "kind": "real", "text": text, "accept": accept, "say_re": say_re, "counts": {},
                             "has_wake": i == 0})
    for i, t in enumerate(TO_ROBOT + CHATTER):
        f = f"addr_{i:02d}.wav"
        write(out / f, synth(voice, t))
        manifest.append({"file": f, "kind": "addr", "text": t, "to_robot": i < len(TO_ROBOT)})
    (out / "manifest.json").write_text(json.dumps(manifest, indent=1))
    print(f"{len(manifest)} clips in {out}")


if __name__ == "__main__":
    main()
