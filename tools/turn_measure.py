"""T9 measurement (docs/HARNESS_BAKEOFF.md): run a turn skill 3x at count 1 and 1x at count 3, record tonyd's
max_tilt_deg, falls, and how far Tony turned (yaw from before/after head-camera snapshots: ORB feature matches,
undistorted with data/camera_calibration.npz; left = positive). Operator-side, no LLM. Moves the robot: run under
`flock /run/lock/tonypi-robot.lock`. Falls are left to the harness's body keeper (waits up to 45 s for it).

    uv run python tools/turn_measure.py turn_left turn_left_hermes
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import cv2
import numpy as np

from tonypi.tonyd.client import TonydClient

CAL = np.load(Path(__file__).resolve().parents[1] / "data/camera_calibration.npz")
K, DIST = CAL["mtx_array"], CAL["dist_array"]
TRIALS = (1, 1, 1, 3)


def frame(t: TonydClient) -> np.ndarray:
    time.sleep(0.8)  # a fresh frame after the body stopped
    return cv2.imread(t.snapshot()["path"], cv2.IMREAD_GRAYSCALE)


def yaw_deg(before: np.ndarray, after: np.ndarray) -> tuple[float | None, int]:
    """Median change in horizontal bearing of matched features; + = Tony turned left. (None, n) if too few."""
    orb = cv2.ORB_create(1500)
    k1, d1 = orb.detectAndCompute(before, None)
    k2, d2 = orb.detectAndCompute(after, None)
    if d1 is None or d2 is None:
        return None, 0
    matches = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=True).match(d1, d2)
    if len(matches) < 12:
        return None, len(matches)
    p1 = cv2.undistortPoints(np.float32([k1[m.queryIdx].pt for m in matches]).reshape(-1, 1, 2), K, DIST)
    p2 = cv2.undistortPoints(np.float32([k2[m.trainIdx].pt for m in matches]).reshape(-1, 1, 2), K, DIST)
    d = np.degrees(np.arctan(p2[:, 0, 0]) - np.arctan(p1[:, 0, 0]))  # scene moves right when Tony turns left
    med = float(np.median(d))
    inliers = np.abs(d - med) < 3.0
    if inliers.sum() < 8:
        return None, int(inliers.sum())
    return round(float(np.median(d[inliers])), 1), int(inliers.sum())


def wait_up(t: TonydClient, limit_s: float = 45) -> float | None:
    """Seconds until Tony stands again (the keeper's get_up), None if he doesn't."""
    t0 = time.monotonic()
    while time.monotonic() - t0 < limit_s:
        st = t.state(fresh=True)
        if not st["lying"] and not st["busy"] and abs(st["tilt_deg"] or 0) < 20:
            return round(time.monotonic() - t0, 1)
        time.sleep(0.5)
    return None


def measure(t: TonydClient, skill: str) -> dict:
    trials = []
    for count in TRIALS:
        t.head(0, 0)
        before = frame(t)
        job = t.run(skill, count)
        time.sleep(2.5)  # the keeper settles ~0.8 s after the move
        st = t.state(fresh=True)
        fell = bool(st["lying"]) or (job.get("max_tilt_deg") or 0) > 60
        rec = {"count": count, "state": job["state"], "max_tilt_deg": job.get("max_tilt_deg"), "fell": fell}
        if fell:
            rec["recovered_in_s"] = wait_up(t)
            rec["yaw_deg"] = None  # heading after a fall + get_up isn't the turn's
        else:
            rec["yaw_deg"], rec["matches"] = yaw_deg(before, frame(t))
            if rec["yaw_deg"] is not None:
                rec["deg_per_step"] = round(rec["yaw_deg"] / count, 1)
        trials.append(rec)
        print(json.dumps({"skill": skill, **rec}), flush=True)
    tilts = [r["max_tilt_deg"] for r in trials if r["max_tilt_deg"] is not None]
    steps = [r["deg_per_step"] for r in trials if r.get("deg_per_step") is not None]
    return {"skill": skill, "max_tilt_mean": round(sum(tilts) / len(tilts), 1) if tilts else None,
            "max_tilt_max": max(tilts) if tilts else None, "falls": sum(r["fell"] for r in trials),
            "deg_per_step_median": float(np.median(steps)) if steps else None, "trials": trials}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("skills", nargs="+")
    args = ap.parse_args()
    t = TonydClient()
    for skill in args.skills:
        print(json.dumps({"summary": measure(t, skill)}), flush=True)


if __name__ == "__main__":
    main()
