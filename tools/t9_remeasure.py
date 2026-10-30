"""Gyro re-measurement after T9: every harness's turn_left next to the default, one session, one floor spot,
round-robin (each version once per round), each trial from a settled stand. Hold the robot lock:

  flock /run/lock/tonypi-robot.lock uv run python tools/t9_remeasure.py [rounds]
"""

import json
import statistics
import sys
import time

from tonypi.tonyd.client import TonydClient

# skill -> motion for a temporary skill (None: the skill exists already)
VERSIONS = {
    "turn_left": None,                     # factory default
    "t9_x14_own": "turn_left_v1_own",      # own, uniform x1.4 slower (run 1's adopted version = run 2's v1)
    "t9_best_own": "turn_left_own",        # own, run 2's steadiest candidate (x1.4 + 210 ms pre-lift), not adopted
    "turn_left_pi": None,                  # pi: frames x1.3, ankle rolls 1/9 up to 10 counts toward 500
    "turn_left_hermes": None,              # Hermes: same recipe
}


def trial(c: TonydClient, skill: str) -> dict:
    fell = False
    if c.state()["lying"]:
        c.run("get_up")
    c.run("settle")
    time.sleep(0.5)
    job = c.run(skill, 1)
    if c.state()["lying"]:
        fell = True
        c.run("get_up")
    return {"skill": skill, "state": job["state"], "yaw_deg": job.get("yaw_deg"),
            "max_tilt_deg": job.get("max_tilt_deg"), "fell": fell}


def stats(xs: list) -> dict:
    xs = [x for x in xs if x is not None]
    if not xs:
        return {}
    return {"mean": round(statistics.mean(xs), 1), "sd": round(statistics.stdev(xs), 1) if len(xs) > 1 else 0.0,
            "min": min(xs), "max": max(xs), "n": len(xs)}


def main() -> None:
    rounds = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    c = TonydClient()
    c.head(0, 0)
    temp = [k for k, m in VERSIONS.items() if m]
    for k in temp:
        c.call("skill_save", name=k, motions=[f"*{VERSIONS[k]}"], description="temporary: T9 gyro re-measurement")
    trials = []
    try:
        for r in range(rounds):
            for skill in VERSIONS:
                t = {"round": r + 1, **trial(c, skill)}
                trials.append(t)
                print(json.dumps(t), flush=True)
        c.run("settle")
    finally:
        for k in temp:
            c.call("skill_delete", name=k)
    out = {k: {"yaw": stats([t["yaw_deg"] for t in trials if t["skill"] == k]),
               "tilt": stats([t["max_tilt_deg"] for t in trials if t["skill"] == k]),
               "falls": sum(t["fell"] for t in trials if t["skill"] == k)} for k in VERSIONS}
    print("RESULT", json.dumps(out), flush=True)


if __name__ == "__main__":
    main()
