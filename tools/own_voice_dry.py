"""Voice dry runs for the E2E test: speak a command into tonypi-voice (espeak-ng WAV, `inject`), let the harness
do it, record the harness's task log (one per category) for `tonypi-harness --check`. Needs tonypi-voice running
and the harness started with --voice (tools/own_slot.sh does both).

  uv run python tools/own_voice_dry.py find "find the door" walk "walk to the chair" motion "do a jumping jack"
"""

import json
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HARNESS = "/run/tonypi/harness.sock"
VOICE = "/run/tonypi/voice.sock"
RUNS = Path.home() / ".local/state/tonypi/harness-own/dry-runs.json"


def inject(wav: str) -> dict:
    s = socket.socket(socket.AF_UNIX)
    s.connect(VOICE)  # no attach: the harness is the attached brain
    s.sendall((json.dumps({"op": "inject", "wav": wav, "rid": 1}) + "\n").encode())
    return json.loads(s.makefile().readline())


def run(category: str, text: str, timeout: float = 420) -> dict:
    wav = tempfile.NamedTemporaryFile(suffix=".wav", delete=False).name
    subprocess.run(["espeak-ng", "-s", "140", "-w", wav, f"Tony, {text}"], check=True)
    h = socket.socket(socket.AF_UNIX)
    h.connect(HARNESS)
    events = h.makefile("r")
    t0 = time.time()
    print(f"## {category}: {text!r} -> inject {inject(wav)}", flush=True)
    rec = {"category": category, "spoken": text, "when": time.strftime("%Y-%m-%dT%H:%M:%S")}
    h.settimeout(timeout)
    for line in events:
        e = json.loads(line)
        if e["type"] in ("heard", "say", "ask", "fall", "stuck") or (e["type"] == "action" and e.get("name")):
            print(f'{e["t"] - t0:6.1f} {e["type"]} {e.get("name") or ""} {(e.get("text") or "")[:160]}', flush=True)
        if e["type"] == "heard":
            rec["heard"] = e["text"]
            rec["heard_after_s"] = round(e["t"] - t0, 2)
        if e["type"] == "task_log":
            rec.update({k: v for k, v in e.items() if k not in ("type", "t")})
            break
    runs = json.loads(RUNS.read_text()) if RUNS.exists() else {}
    runs[f"voice-{category}"] = rec
    RUNS.write_text(json.dumps(runs, indent=1))
    return rec


if __name__ == "__main__":
    a = sys.argv[1:]
    for cat, text in zip(a[::2], a[1::2]):
        print(json.dumps(run(cat, text)), flush=True)
        time.sleep(4)
