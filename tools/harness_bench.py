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
# T9 (docs/HARNESS_BAKEOFF.md): the brain fixes its own turn. Measured by this script before and after, the same way
# for every harness; the brain's run in between is capped at 15 min / 40 turn steps.
TASKS["T9"] = ("Your left turn keeps tipping you over. Improve turn_left: measure the default, change it in small "
               "steps, test, and save your best version as turn_left_own. Don't change the default turn_left.", [])
T9_LIMIT_S, T9_LIMIT_STEPS, T9_TRIALS = 15 * 60, 40, [1, 1, 1, 3]
FX = 424.6  # camera focal length in px (data/camera_calibration.npz): yaw = atan(image shift / FX)
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


def yaw_deg(before: bytes, after: bytes) -> tuple[float | None, float]:
    """How far the view turned (degrees, + = left turn: the scene moves right) from two snapshots: ORB features
    matched and a RANSAC similarity fit; the horizontal shift is the yaw. None below 15 inliers. Second value: the
    inlier count. (Phase correlation was tried first: on real turns it locked onto rug patterns and flipped sign.)"""
    import math

    import cv2
    import numpy as np
    a, b = (cv2.imdecode(np.frombuffer(x, np.uint8), cv2.IMREAD_GRAYSCALE) for x in (before, after))
    orb = cv2.ORB_create(1500)
    ka, da = orb.detectAndCompute(a, None)
    kb, db = orb.detectAndCompute(b, None)
    if da is None or db is None:
        return None, 0
    matches = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=True).match(da, db)
    if len(matches) < 15:
        return None, len(matches)
    pa = np.float32([ka[m.queryIdx].pt for m in matches])
    pb = np.float32([kb[m.trainIdx].pt for m in matches])
    M, inl = cv2.estimateAffinePartial2D(pa, pb, method=cv2.RANSAC, ransacReprojThreshold=4)
    n = int(inl.sum()) if inl is not None else 0
    if M is None or n < 15:
        return None, n
    # the shift of the image centre under the fitted transform (rotation about the optical axis = body roll)
    cx, cy = a.shape[1] / 2, a.shape[0] / 2
    dx = M[0, 0] * cx + M[0, 1] * cy + M[0, 2] - cx
    return round(math.degrees(math.atan(dx / FX)), 1), n


SNAP_DIR = Path(os.environ.get("TONYPI_BENCH_SNAPS", "/home/pi/.local/state/tonypi/bench-snaps"))


def snap(tonyd: TonydClient, name: str = "") -> bytes:
    time.sleep(0.6)  # a still frame
    jpeg = Path(tonyd.snapshot()["path"]).read_bytes()
    if name:  # kept, so the yaw numbers can be checked by eye
        SNAP_DIR.mkdir(parents=True, exist_ok=True)
        (SNAP_DIR / f"{name}.jpg").write_bytes(jpeg)
    return jpeg


def turn_trials(tonyd: TonydClient, skill: str, trials: list[int] = T9_TRIALS) -> list[dict]:
    """T9 measurement, each trial from a settled stand: max tilt, yaw from the gyro (tonyd's per-job yaw_deg, + =
    left; since main 7478f56) and, as a cross-check, from the photos before/after."""
    out = []
    tonyd.head(0, 15)  # a bit up: furniture and walls to match, not the rug right in front of the feet
    stamp = time.strftime("%H%M%S")
    for i, n in enumerate(trials):
        fell = False
        if tonyd.state()["lying"]:
            tonyd.run("get_up")
        tonyd.run("settle")
        a = snap(tonyd, f"{stamp}-{skill}-{i}-before")
        job = tonyd.run(skill, n)
        if tonyd.state()["lying"]:
            fell = True
            tonyd.run("get_up")
        tonyd.run("settle")
        yaw, inliers = yaw_deg(a, snap(tonyd, f"{stamp}-{skill}-{i}-after"))
        out.append({"skill": skill, "count": n, "state": job["state"], "max_tilt_deg": job.get("max_tilt_deg"),
                    "yaw_deg": job.get("yaw_deg"), "yaw_photo_deg": yaw, "inliers": inliers, "fell": fell,
                    "said": job.get("said")})
        print(json.dumps(out[-1]), flush=True)
    return out


def summary(trials: list[dict]) -> dict:
    ones = [t for t in trials if t["count"] == 1]
    yaws = [t["yaw_deg"] for t in ones if t["yaw_deg"] is not None]
    tilts = [t["max_tilt_deg"] for t in ones if t["max_tilt_deg"] is not None]
    return {"max_tilt_count1": [t["max_tilt_deg"] for t in ones],
            "mean_tilt_count1": round(sum(tilts) / len(tilts), 1) if tilts else None,
            "max_tilt_count3": [t["max_tilt_deg"] for t in trials if t["count"] == 3],
            "deg_per_step": round(sum(yaws) / len(yaws), 1) if yaws else None,
            "deg_count3": [t["yaw_deg"] for t in trials if t["count"] == 3],
            "falls": sum(t["fell"] for t in trials)}


def run_t9(c: Conn, tonyd: TonydClient, label: str) -> dict:
    t_start = time.time()
    before = turn_trials(tonyd, "turn_left")
    n0 = len(c.events)
    t0 = c.send(TASKS["T9"][0])
    rid = (c.wait(lambda e: e["type"] == "accepted", 5, n0) or {}).get("id")
    steps, stopped = 0, None
    while not c.wait(lambda e, i=rid: e["type"] == "done" and e.get("id") == i, 1.0, n0):
        acts = [e for e in c.events[n0:] if e["type"] == "action"]
        steps = sum(int(e.get("count") or 1) for e in acts if "turn" in str(e.get("name")) or e["name"] == "play_frames")
        if stopped is None and (steps > T9_LIMIT_STEPS or time.time() - t0 > T9_LIMIT_S):
            stopped = f"limit: {steps} turn steps, {time.time() - t0:.0f} s"
            c.send("stop")
        if time.time() - t0 > T9_LIMIT_S + 120:
            break
    brain_s = time.time() - t0
    evs = c.events[n0:]
    acts = [e for e in evs if e["type"] == "action"]
    results = [e for e in evs if e["type"] == "tool_result"]
    saved = "turn_left_own" in tonyd.call("skills")["details"]
    after = turn_trials(tonyd, "turn_left_own") if saved else []
    return {
        "task": "T9", "label": label, "t": round(t0), "brain_s": round(brain_s), "total_s": round(time.time() - t_start),
        "stopped_by_limit": stopped, "turn_steps": steps, "tool_calls": len(acts),
        "falls_during_run": sum("FELL" in (e.get("text") or "") for e in results),
        "get_ups": sum(e.get("name") == "get_up" for e in acts),
        "tiers": [e["text"][11:] for e in evs if e["type"] == "info" and e["text"].startswith("codex tier ")],
        "said": [e["text"] for e in evs if e["type"] == "say"],
        "tokens_in": sum((u.get("input") or 0) + (u.get("cache_read") or 0) for u in evs if u["type"] == "usage"),
        "tokens_out": sum(u.get("output") or 0 for u in evs if u["type"] == "usage"),
        "saved": saved, "own_skill": tonyd.call("skills")["details"].get("turn_left_own"),
        "before": summary(before), "after": summary(after) if after else None,
        "trials_before": before, "trials_after": after,
        "brain_actions": [(round(e["t"] - t0, 1), e["name"], e.get("count"),
                           None if e["name"] == "play_frames" else e.get("args")) for e in acts],
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("tasks", nargs="+", choices=sorted(TASKS))
    ap.add_argument("--label", default="", help="free text stored with each result (e.g. 'claude opus low')")
    ap.add_argument("--timeout", type=float, default=150)
    args = ap.parse_args()
    c, tonyd = Conn(), TonydClient()
    for name in args.tasks:
        r = run_t9(c, tonyd, args.label) if name == "T9" else run_task(c, tonyd, name, args.timeout)
        r["label"] = args.label
        print(json.dumps(r), flush=True)
        with OUT.open("a") as f:
            f.write(json.dumps(r) + "\n")
        time.sleep(3)


if __name__ == "__main__":
    main()
