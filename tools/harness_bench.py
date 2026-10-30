"""Bake-off benchmark T1-T8 against a running tonypi-harness (text front end), see docs/HARNESS_BAKEOFF.md.

  uv run python tools/harness_bench.py T1 T2 T5          # read-only-ish tasks
  flock /run/lock/tonypi-robot.lock uv run python tools/harness_bench.py T1 T3 T4 T6   # moves the robot

Prints one line per task and appends raw results to docs/bakeoff/own-runs.jsonl. Times are seconds from sending the
command. first_action = first tool call that acts or senses (not say); steering (T4) = from the correction to the
first move that follows it; stop (T8) = from "stop" to tonyd reporting idle.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import threading
import time
from pathlib import Path

from tonypi.tonyd.client import TonydClient

SOCK = os.environ.get("TONYPI_HARNESS_SOCK", "/run/tonypi/harness.sock")
OUT = Path(__file__).resolve().parent.parent / "docs/bakeoff/own-runs.jsonl"

TASKS = {
    "T1": ("wave", []),
    "T2": ("what do you see?", []),
    "T3": ("look left and tell me if there's a chair", []),
    "T4": ("walk forward 3 steps, then turn left", [(2.0, "actually, turn right instead")]),
    "T5": ("how's your battery and how hot are you?", []),
    "T6": ("find the door and walk toward it", [(60.0, "stop")]),
}
TASKS["T8"] = ("find the door and walk toward it", [("walk+1.5", "stop")])  # stop 1.5 s into its first walk
# T4 with the correction 1.5 s after the first step started (T4's fixed 2 s can land before the walk begins)
TASKS["T4m"] = ("walk forward 3 steps, then turn left", [("move+1.5", "actually, turn right instead")])
# movement authoring (2026-10-29: Tony fell three times during turns): the brain tunes its own turn_left
TASKS["T9"] = ("Your turn_left has made you fall over today. Make it steadier: first measure the default with two "
               "single turns, then try small changes one at a time, and only adopt a version that has a clearly lower "
               "max tilt over two trials and still turns. Save your versions with the suffix _own (adopt it as "
               "turn_left_own; leave turn_left itself alone). Tell me what you changed and the numbers.", [])
# T8 without the camera: "stop" 1 s into a walk
TASKS["T8w"] = ("walk forward 5 steps", [("move+1.0", "stop")])


class Conn:
    def __init__(self) -> None:
        self.s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.s.connect(SOCK)
        self.events: list[dict] = []
        self.lock = threading.Condition()
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        for line in self.s.makefile("r", encoding="utf-8"):
            ev = json.loads(line)
            ev["rx"] = time.time()
            with self.lock:
                self.events.append(ev)
                self.lock.notify_all()

    def send(self, text: str) -> float:
        t = time.time()
        self.s.sendall((json.dumps({"text": text}) + "\n").encode())
        return t

    def wait(self, pred, timeout: float, start: int = 0) -> dict | None:
        end = time.time() + timeout
        with self.lock:
            while True:
                for ev in self.events[start:]:
                    if pred(ev):
                        return ev
                left = end - time.time()
                if left <= 0:
                    return None
                self.lock.wait(left)


def procs() -> list[int]:
    """The harness process and all its descendants (the brain CLI)."""
    try:
        pid = int(subprocess.check_output(["pgrep", "-f", "-o", r"bin/python3? .*tonypi-harness( |$)"]).split()[0])
    except (subprocess.CalledProcessError, IndexError):
        return []
    out, todo = [], [pid]
    while todo:  # the whole tree: codex app-server and its code-mode host
        p = todo.pop()
        out.append(p)
        todo += [int(k) for k in subprocess.run(["pgrep", "-P", str(p)], capture_output=True, text=True).stdout.split()]
    return out


def sample(pids: list[int]) -> tuple[int, float]:
    """(RSS MB, CPU seconds) summed over pids."""
    rss, cpu = 0, 0.0
    tick = os.sysconf("SC_CLK_TCK")
    for p in pids:
        try:
            st = Path(f"/proc/{p}/stat").read_text().rsplit(")", 1)[1].split()
            cpu += (int(st[11]) + int(st[12])) / tick
            rss += int(Path(f"/proc/{p}/statm").read_text().split()[1]) * os.sysconf("SC_PAGE_SIZE")
        except (OSError, IndexError):
            pass
    return rss // 2**20, cpu


def llm_load() -> int | None:
    """llama-server CPU % over 1 s (shared with the other bake-off sessions: >50 % means someone else is using it)."""
    try:
        pid = int(subprocess.check_output(["pgrep", "-o", "llama-server"]).split()[0])
    except (subprocess.CalledProcessError, IndexError):
        return None
    a = sample([pid])[1]
    time.sleep(1)
    return round((sample([pid])[1] - a) * 100)


def run_task(c: Conn, tonyd: TonydClient, name: str, timeout: float) -> dict:
    text, steps = TASKS[name]
    llm_before = llm_load()
    pids = procs()
    rss0, cpu0 = sample(pids)
    peak = rss0
    n0 = len(c.events)
    t0 = c.send(text)
    rid = c.wait(lambda e: e["type"] == "accepted", 5, n0)
    rid = rid and rid.get("id")
    sent = []
    for delay, words in steps:
        if isinstance(delay, str):  # "move+S" / "walk+S": S seconds after the first action / walk started
            want = delay.split("+")[0]
            first = c.wait(lambda e: e["type"] == "action" and e.get("name") not in ("stop",) and (
                want == "move" or str(e.get("name")).startswith(want)), timeout, n0)
            delay = (first["t"] - t0 if first else 0) + float(delay.split("+")[1])
        while time.time() < t0 + delay:
            peak = max(peak, sample(pids)[0])
            if c.wait(lambda e, i=rid: e["type"] == "done" and e.get("id") == i, 0.2, n0):
                break
        ts = c.send(words)
        sent.append((words, ts))
        if words == "stop":  # T8: time until the body is idle
            while tonyd.state()["busy"] and time.time() < ts + 5:
                time.sleep(0.01)
            sent[-1] += (time.time() - ts,)
    while not c.wait(lambda e, i=rid: e["type"] == "done" and e.get("id") == i, 0.5, n0):
        peak = max(peak, sample(pids)[0])
        if time.time() - t0 > timeout:
            break
    t_end = time.time()
    rss1, cpu1 = sample(pids)
    evs = c.events[n0:]
    acts = [e for e in evs if e["type"] == "action" and e.get("name") not in ("stop",)]
    says = [e for e in evs if e["type"] == "say"]
    usage = [e for e in evs if e["type"] == "usage"]
    r = {
        "task": name, "text": text, "t": round(t0),
        "first_action_s": round(acts[0]["t"] - t0, 2) if acts else None,
        "first_say_s": round(says[0]["t"] - t0, 2) if says else None,
        "total_s": round(t_end - t0, 2),
        "actions": [(round(e["t"] - t0, 1), e["name"], e.get("count"), e.get("args")) for e in acts],
        "said": [e["text"] for e in says],
        "brains": sorted({e["text"][7:] for e in evs if e["type"] == "info" and e["text"].startswith("brain: ")}),
        "tiers": [e["text"][11:] for e in evs if e["type"] == "info" and e["text"].startswith("codex tier ")],
        "refused": [e["name"] for e in evs if e["type"] == "refused"],
        "cost_usd": round(sum(u.get("cost_usd") or 0 for u in usage), 4),
        "tokens_in": sum((u.get("input") or 0) + (u.get("cache_read") or 0) + (u.get("cache_write") or 0)
                         for u in usage),
        "tokens_out": sum(u.get("output") or 0 for u in usage),
        "rss_mb": peak, "cpu_s": round(cpu1 - cpu0, 1),
        "timed_out": t_end - t0 > timeout, "llm_cpu_pct_before": llm_before,
    }
    for words, ts, *extra in sent:
        if words == "stop":
            r["stop_to_idle_s"] = round(extra[0], 3) if extra else None
            r["moves_after_stop"] = [e["name"] for e in acts if e["t"] > ts + 0.05]
        else:
            after = [e for e in acts if e["t"] > ts]
            r["steer_text"] = words
            r["steer_to_first_move_s"] = round(after[0]["t"] - ts, 2) if after else None
            r["moves_after_steer"] = [(e["name"], e.get("count")) for e in after]
    return r


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("tasks", nargs="+", choices=sorted(TASKS))
    ap.add_argument("--label", default="", help="free text stored with each result (e.g. 'claude opus low')")
    ap.add_argument("--timeout", type=float, default=150)
    args = ap.parse_args()
    c, tonyd = Conn(), TonydClient()
    for name in args.tasks:
        r = run_task(c, tonyd, name, args.timeout)
        r["label"] = args.label
        print(json.dumps(r), flush=True)
        with OUT.open("a") as f:
            f.write(json.dumps(r) + "\n")
        time.sleep(3)


if __name__ == "__main__":
    main()
