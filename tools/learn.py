#!/usr/bin/env python3
"""Body learning from the shell (the same verbs as the tonypi-mcp tools), for background workers and Claude.

  uv run python tools/learn.py trial --skill turn_left --count 2 --trials 5 --objective turn
  uv run python tools/learn.py trial --base go_forward --gait '{"time_scale": 0.9}' --count 3 --objective walk
  uv run python tools/learn.py compare turn_left '{"base": "turn_left", "gait": {"amplitude": 1.2}}' --trials 5 \\
      --objective turn
  uv run python tools/learn.py sweep [--joints 4 12] [--standing]
  uv run python tools/learn.py ledger [--skill turn_left] [--limit 20] | body-model | library
  uv run python tools/learn.py gait go_forward --gait '{"mirror": true}'    # print candidate frames, no robot

Moves the robot (trial, compare, sweep): takes /run/lock/tonypi-robot.lock itself and refuses if it is held
(TONYPI_ROBOT_LOCK_HELD=1 when the caller already holds it). Prints JSON.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _json(text: str | None):
    if text is None:
        return None
    try:
        return json.loads(text)
    except ValueError:
        return text  # a bare skill name


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="verb", required=True)
    t = sub.add_parser("trial", help="measured trials of one move")
    src = t.add_mutually_exclusive_group(required=True)
    src.add_argument("--skill")
    src.add_argument("--frames", help="JSON keyframes or @file.json")
    src.add_argument("--base", help="a motion name, transformed by --gait")
    t.add_argument("--gait", type=json.loads, help='JSON: {"time_scale", "amplitude", "offset", "mirror", "repeat"}')
    t.add_argument("--count", type=int, default=1)
    t.add_argument("--trials", type=int, default=3)
    t.add_argument("--guard", type=float, dest="guard_tilt_deg")
    t.add_argument("--objective", default="generic")
    t.add_argument("--label")
    t.add_argument("--trace", action="store_true")
    t.add_argument("--surface", help="e.g. rug, wood")
    t.add_argument("--start", choices=("stand", "as_is"), help="stand (default: get up, settle) or as_is")
    t.add_argument("--setup", help="skill run before each trial, unmeasured (e.g. lie_down; implies --start as_is)")
    t.add_argument("--budget-s", type=float)
    c = sub.add_parser("compare", help="interleaved A/B trials with a promote verdict")
    c.add_argument("base", help="skill name or JSON spec")
    c.add_argument("candidate", help="skill name or JSON spec")
    c.add_argument("--trials", type=int, default=5)
    c.add_argument("--objective")
    c.add_argument("--budget-s", type=float)
    s = sub.add_parser("sweep", help="joint map (lying: legs; --standing: arms)")
    s.add_argument("--joints", type=int, nargs="*")
    s.add_argument("--standing", action="store_true")
    s.add_argument("--budget-s", type=float)
    lg = sub.add_parser("ledger")
    lg.add_argument("--skill")
    lg.add_argument("--label")
    lg.add_argument("--limit", type=int, default=20)
    sub.add_parser("body-model")
    sub.add_parser("library")
    g = sub.add_parser("gait", help="print a candidate's frames (base motion + gait params); no robot moves")
    g.add_argument("base")
    g.add_argument("--gait", type=json.loads)
    a = ap.parse_args(argv)

    from tonypi.learn.runner import Lab, LabError
    from tonypi.tonyd.client import TonydError

    try:
        if a.verb == "gait":
            from tonypi.learn import gait
            from tonypi.tonyd.client import TonydClient

            out = gait.apply(TonydClient().call("motion_get", name=a.base), a.gait)
            out["hash"] = gait.digest(out)
        elif a.verb in ("ledger", "body-model", "library"):
            lab = Lab(use_phone=False)
            out = (lab.ledger(a.skill, a.label, a.limit) if a.verb == "ledger"
                   else lab.body_model() if a.verb == "body-model" else lab.library())
        elif a.verb == "trial":
            frames = a.frames
            if frames and frames.startswith("@"):
                frames = Path(frames[1:]).read_text()
            spec = {"skill": a.skill, "frames": json.loads(frames) if frames else None, "base": a.base, "gait": a.gait,
                    "count": a.count, "guard_tilt_deg": a.guard_tilt_deg, "objective": a.objective, "label": a.label,
                    "trace": a.trace, "surface": a.surface, "start": a.start, "setup": a.setup}
            out = Lab().batch(spec, a.trials, budget_s=a.budget_s)
        elif a.verb == "compare":
            out = Lab().compare(_json(a.base), _json(a.candidate), a.trials, a.objective, budget_s=a.budget_s)
        else:
            out = Lab().sweep(a.joints, lying=not a.standing, budget_s=a.budget_s)
    except (LabError, TonydError, ValueError) as e:
        print(json.dumps({"error": str(e)}))
        return 1
    print(json.dumps(out, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
