"""Scripted commands to the pi front end (tonypi-pi), with timestamps: the bake-off's text benchmarks.

  python tools/pi_bench.py "wave"
  python tools/pi_bench.py "walk forward 3 steps, then turn left" "@2" "actually, turn right instead"
  python tools/pi_bench.py --timeout 70 "find the door and walk toward it" "@60" "stop"

"@N" waits until N s after the start before the next command. Exits when the last command is done.
--voice says each command out loud ("Hey Tony, <command>") through the robot's own speaker from this process (the
front end doesn't know it's us, so its mic hears it through the room): the real wake word + ASR path.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import threading
import time

SOCK = os.environ.get("TONYPI_PI_SOCK", "/run/tonypi/pi.sock")


VOLUME = float(os.environ.get("TONYPI_BENCH_VOLUME", "0.15"))  # the mic is right next to the speaker: full volume clips


def quiet_say(speaker, text: str) -> None:
    import numpy as np

    pcm = b"".join(c.audio_int16_bytes for c in speaker.voice.synthesize(text))
    pcm = (np.frombuffer(pcm, dtype=np.int16) * VOLUME).astype(np.int16).tobytes()
    speaker._play([pcm], speaker.rate)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("script", nargs="+")
    ap.add_argument("--timeout", type=float, default=240)
    ap.add_argument("--voice", action="store_true")
    args = ap.parse_args()
    speaker = None
    if args.voice:
        from tonypi.agent.speech import Speaker
        speaker = Speaker()
        speaker.say = lambda text: quiet_say(speaker, text)
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.connect(SOCK)
    t0 = time.monotonic()
    ids: list[int] = []
    done: set[int] = set()
    last_done = threading.Event()
    n_cmds = sum(1 for x in args.script if not x.startswith("@"))

    def reader() -> None:
        for line in s.makefile("r", encoding="utf-8"):
            ev = json.loads(line)
            t = ev.pop("type")
            if t == "accepted":
                ids.append(ev["id"])
                if len(ids) == n_cmds and ids[-1] in done:  # "done" can arrive before "accepted" (stop)
                    last_done.set()
                continue
            if t == "heard" and args.voice and ev.get("source") == "voice":
                ids.append(ev["id"])
            ev.pop("t", None)
            print(f"{time.monotonic() - t0:7.2f}  {t:7s} {json.dumps(ev)[:240]}", flush=True)
            if t == "done":
                done.add(ev.get("id"))
            if t == "done" and len(ids) == n_cmds and ev.get("id") == ids[-1]:
                last_done.set()

    threading.Thread(target=reader, daemon=True).start()
    for item in args.script:
        if item.startswith("@"):
            time.sleep(max(0.0, t0 + float(item[1:]) - time.monotonic()))
            continue
        print(f"{time.monotonic() - t0:7.2f}  SEND    {item}", flush=True)
        if speaker:
            speaker.say(f"Hey Tony, {item}")
            print(f"{time.monotonic() - t0:7.2f}  SPOKEN  (end of speech)", flush=True)
        else:
            s.sendall((json.dumps({"text": item}) + "\n").encode())
    if not last_done.wait(args.timeout):
        print(f"timeout after {args.timeout} s")


if __name__ == "__main__":
    main()
