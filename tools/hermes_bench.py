"""Bake-off tasks T1-T6 + T8 through the Hermes front end (docs/HARNESS_BAKEOFF.md). Moves the robot: run under
`flock /run/lock/tonypi-robot.lock`, robot on the floor.

    uv run python tools/hermes_bench.py [T1 T2 ...] [--sock /run/user/1000/tonypi-hermes.sock] [--label terra-none]

Timing comes from tonyd's own job events (first motion = first job started) and the front end's events.
Prints one JSON line per task.
"""

from __future__ import annotations

import argparse
import json
import socket
import threading
import time

from tonypi.tonyd import SOCK as TONYD_SOCK

TASKS = {
    "T1": "wave",
    "T2": "what do you see?",
    "T3": "look left and tell me if there's a chair",
    "T4": "walk forward 3 steps, then turn left",
    "T5": "how's your battery and how hot are you?",
    "T6": "find the door and walk toward it",
    "T9": "Your turns keep tipping you over. Fix turn_left: tune it, test it, and save your version as turn_left_hermes.",
}
WAIT_S = {"T9": 1200}  # per-task limit for the whole turn (default 300 s)
STEER_T4 = "actually, turn right instead"
T6_LIMIT_S = 60  # then "stop" (T8)


class Bench:
    def __init__(self, sock: str) -> None:
        self.front = socket.socket(socket.AF_UNIX)
        self.front.connect(sock)
        self.events: list[tuple[float, dict]] = []
        self.jobs: list[tuple[float, dict]] = []
        self.cond = threading.Condition()
        threading.Thread(target=self._read, args=(self.front, self.events), daemon=True).start()
        body = socket.socket(socket.AF_UNIX)
        body.connect(TONYD_SOCK)
        body.sendall(b'{"op": "subscribe"}\n')
        threading.Thread(target=self._read, args=(body, self.jobs), daemon=True).start()

    def _read(self, s: socket.socket, out: list) -> None:
        for line in s.makefile("r", encoding="utf-8"):
            ev = json.loads(line)
            if out is self.jobs and ev.get("event") != "job":
                continue
            with self.cond:
                out.append((time.monotonic(), ev))
                self.cond.notify_all()

    def send(self, text: str) -> None:
        self.front.sendall((json.dumps({"text": text}) + "\n").encode())

    def wait(self, pred, timeout: float) -> bool:
        with self.cond:
            return self.cond.wait_for(pred, timeout)

    def run(self, task: str) -> dict:
        text = TASKS[task]
        self.events.clear(), self.jobs.clear()
        t0 = time.monotonic()
        self.send(text)
        started = lambda: [t for t, j in self.jobs if j.get("state") == "running"]  # noqa: E731
        extra: dict = {}
        if task == "T4":  # steer 2 s into the first motion
            self.wait(lambda: started(), 120)
            time.sleep(2)
            ts = time.monotonic()
            self.send(STEER_T4)
            extra["steer_at_s"] = round(ts - t0, 1)
            self.wait(lambda: any(j.get("skill") == "turn_right" and j.get("state") == "running"
                                  for _, j in self.jobs), 120)
            tr = [t for t, j in self.jobs if j.get("skill") == "turn_right" and j.get("state") == "running"]
            extra["steer_to_turn_right_s"] = round(tr[0] - ts, 1) if tr else None
            extra["cut_after_steer_s"] = next((round(t - ts, 2) for t, j in self.jobs
                                               if t >= ts and j.get("state") == "cancelled"), None)
        if task == "T6":  # open-ended: stop after a minute (= T8)
            done = self.wait(lambda: any(e.get("type") == "done" for _, e in self.events), T6_LIMIT_S)
            if not done:
                running = [j for _, j in self.jobs if j.get("state") == "running"]
                ts = time.monotonic()
                self.send("stop")
                self.wait(lambda: any(t >= ts and j.get("state") in ("cancelled", "done") for t, j in self.jobs)
                          or not running, 10)
                cut = [t for t, j in self.jobs if t >= ts and j.get("state") in ("cancelled", "done")]
                extra["T8_stop_to_body_stopped_s"] = round(cut[0] - ts, 2) if cut else "idle when stopped"
        self.wait(lambda: sum(e.get("type") == "done" for _, e in self.events) >= (2 if task == "T4" else 1)
                  or (task == "T6" and "T8_stop_to_body_stopped_s" in extra
                      and any(e.get("type") == "done" for _, e in self.events)), WAIT_S.get(task, 300))
        end = max((t for t, e in self.events if e.get("type") == "done"), default=time.monotonic())
        tools = [(round(t - t0, 1), e.get("name"), e.get("preview", "")[:60]) for t, e in self.events
                 if e.get("type") == "action"]
        info = [e["text"] for _, e in self.events if e.get("type") == "info"]
        first_tool = tools[0][0] if tools else None
        st = started()
        return {"task": task, "text": text,
                "first_action_s": first_tool, "first_motion_s": round(st[0] - t0, 1) if st else None,
                "total_s": round(end - t0, 1),
                "said": [e["text"] for _, e in self.events if e.get("type") == "say"],
                "tools": tools, "jobs": [(round(t - t0, 1), j.get("skill"), j.get("count"), j.get("state"),
                                           j.get("max_tilt_deg")) for t, j in self.jobs],
                "body_notes": [e["text"] for _, e in self.events
                               if e.get("type") == "info" and "Tony's body" in e.get("text", "") or
                               e.get("type") == "info" and "fell" in e.get("text", "")],
                "brain": next((i for i in info if i.startswith("brain:")), None),
                "usage": next((i for i in info if i.startswith("run.")), None), **extra}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("tasks", nargs="*", default=list(TASKS))
    ap.add_argument("--sock", default="/run/tonypi/agent.sock")
    ap.add_argument("--label", default="")
    args = ap.parse_args()
    b = Bench(args.sock)
    for task in args.tasks:
        r = b.run(task)
        r["label"] = args.label
        print(json.dumps(r), flush=True)
        time.sleep(3)


if __name__ == "__main__":
    main()
