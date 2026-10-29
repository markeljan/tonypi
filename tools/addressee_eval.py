"""Is this audio clip spoken TO the robot? d1 decision models vs LFM3-Omni, on the addr_* clips of tools/voice_set.py.

usage: uv run python tools/addressee_eval.py VOICE_SET_DIR d1 MODEL MMPROJ     (starts llama-server on :8085)
       uv run python tools/addressee_eval.py VOICE_SET_DIR d1-choice MODEL MMPROJ   (who is being addressed?)
       uv run python tools/addressee_eval.py VOICE_SET_DIR omni [URL]          (asks a running Omni server)
Prints per-clip probability/answer, accuracy at 0.5, and time per clip.
"""

import base64
import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

DIR, MODE = Path(sys.argv[1]), sys.argv[2]
QUESTION = ("Is the speaker talking to Tony, a small humanoid robot, giving it a command or asking it something "
            "(not talking to another person or about Tony, and not a TV or radio)?")


def b64(f: Path) -> str:
    return base64.b64encode(f.read_bytes()).decode()


def post(url: str, body: dict) -> dict:
    req = urllib.request.Request(url, json.dumps(body).encode(), {"Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req, timeout=300))


def ask_d1(url: str, f: Path) -> float:
    r = post(url, {"state": "Audio captured by a home robot's microphone.",
                   "files": ["data:audio/wav;base64," + b64(f)],
                   "questions": {"to_robot": {"type": "noul", "instructions": QUESTION}}})
    return float(r["answers"]["to_robot"]["noul"])


CHOICES = {"robot": "Tony, the small humanoid robot (a command or question for it)",
           "person": "Another person in the room", "about": "Someone else, maybe mentioning Tony",
           "media": "Nobody: TV, radio or a recording"}


def ask_d1_choice(url: str, f: Path) -> float:
    r = post(url, {"state": "Audio captured by a home robot's microphone.",
                   "files": ["data:audio/wav;base64," + b64(f)],
                   "questions": {"who": {"type": "choice", "instructions": "Who is the speaker talking to?",
                                         "criteria": CHOICES}}})
    return float(r["answers"]["who"]["probabilities"]["robot"])


def ask_omni(url: str, f: Path) -> float:
    r = post(url, {"messages": [{"role": "system", "content": QUESTION + " Answer yes or no."},
                                {"role": "user", "content": [{"type": "input_audio",
                                                              "input_audio": {"data": b64(f), "format": "wav"}}]}],
                   "max_tokens": 1, "temperature": 0, "logprobs": True, "top_logprobs": 5, "cache_prompt": True,
                   "chat_template_kwargs": {"enable_thinking": False}})
    top = r["choices"][0]["logprobs"]["content"][0]["top_logprobs"]
    import math
    p = {t["token"].strip().lower(): math.exp(t["logprob"]) for t in top}
    yes, no = p.get("yes", 0.0), p.get("no", 0.0)
    return yes / (yes + no) if yes + no else 0.0


def main() -> None:
    clips = [c for c in json.loads((DIR / "manifest.json").read_text()) if c["kind"] in ("addr", "addr_real")]
    srv = None
    if MODE.startswith("d1"):
        model, mmproj = sys.argv[3], sys.argv[4]
        srv = subprocess.Popen(["llama-server", "-m", model, "--mmproj", mmproj, "-t", "4", "-c", "4096", "-b", "4096",
                                "-ub", "4096", "--port", "8085"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        url, ask = "http://127.0.0.1:8085/v1/systemone", (ask_d1_choice if MODE == "d1-choice" else ask_d1)
        t0 = time.monotonic()
        while True:
            try:
                urllib.request.urlopen("http://127.0.0.1:8085/health", timeout=2)
                break
            except Exception:
                time.sleep(0.5)
        print(f"load {time.monotonic() - t0:.1f}s")
    else:
        url, ask = (sys.argv[3] if len(sys.argv) > 3 else "http://127.0.0.1:8080/v1/chat/completions"), ask_omni
    try:
        ok, times = 0, []
        for c in clips:
            t = time.monotonic()
            try:
                p = ask(url, DIR / c["file"])
            except Exception as e:
                p = float("nan")
                print("  error", e)
            dt = time.monotonic() - t
            times.append(dt)
            right = (p >= 0.5) == c["to_robot"]
            ok += right
            print(f"  {'ok' if right else 'x '} p={p:.2f} {dt:.2f}s {'TO ROBOT' if c['to_robot'] else 'chatter '} {c['text']!r}",
                  flush=True)
        if srv:
            rss = int(open(f"/proc/{srv.pid}/status").read().split("VmRSS:")[1].split()[0]) // 1024
            print(f"server RSS {rss} MB")
        times.sort()
        print(f"{MODE}: {ok}/{len(clips)} correct at p>=0.5 | median {times[len(times) // 2]:.2f}s per clip")
    finally:
        if srv:
            srv.terminate()
            srv.wait()


if __name__ == "__main__":
    main()
