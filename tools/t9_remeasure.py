"""Gyro re-measurement after T9: the default turn_left vs the brain's versions, 3 x count 1 each (lock held).

  flock /run/lock/tonypi-robot.lock uv run python tools/t9_remeasure.py
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import harness_bench as h  # noqa: E402

from tonypi.tonyd.client import TonydClient  # noqa: E402

VERSIONS = {"turn_left": None,  # the default
            "t9_x14_own": "turn_left_v1_own",  # uniform x1.4 slower (run 1's adopted version = run 2's v1)
            "t9_best_own": "turn_left_own"}  # run 2's steadiest candidate (x1.4 + 210 ms pre-lift)

c = TonydClient()
out = {}
for skill, motion in VERSIONS.items():
    if motion:
        c.call("skill_save", name=skill, motions=[f"*{motion}"], description="temporary: T9 gyro re-measurement")
    try:
        trials = h.turn_trials(c, skill, [1, 1, 1])
    finally:
        if motion:
            c.call("skill_delete", name=skill)
    yaws = [t["yaw_deg"] for t in trials if t["yaw_deg"] is not None]
    tilts = [t["max_tilt_deg"] for t in trials]
    out[skill] = {"motion": motion or "turn_left (default)", "max_tilt": tilts, "yaw": yaws,
                  "mean_tilt": round(sum(tilts) / len(tilts), 1), "mean_yaw": round(sum(yaws) / len(yaws), 1) if yaws else None,
                  "falls": sum(t["fell"] for t in trials)}
print("RESULT", json.dumps(out))
