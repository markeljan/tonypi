"""Scores a trial from numbers: tonyd's job record, fresh state before and after, and the phone episode (optional).

Every objective returns the same metric fields plus `score` (higher is better) and `basis` (what the score rests on:
"phone" or "steps"). Compare scores only within one objective and basis. The weights follow docs/BODY_LEARNING.md:
  walk     forward m/s - 0.02 |yaw| - 0.5 [max tilt > 25] - 2 fell      (no phone: 5 cm per completed step)
  turn     |yaw| per step - 3 displacement m - 0.1 per tilt deg over 20 - 2 fell
  balance  seconds held without falling - 0.05 max tilt - 2 fell
  pose     -end tilt / 10 - 1 [not done] - 2 fell
  recover  -end tilt / 10 - 0.05 seconds - 1 [not done] - 2 [still lying / tilted past 45 after]
           (get_up from lying: run it with setup "lie_down" or start "as_is"; no default tilt guard)
  generic  -max tilt / 10 - 1 [not done] - 2 fell
"""

from __future__ import annotations

from math import isfinite

OBJECTIVES = ("walk", "turn", "balance", "pose", "recover", "generic")
FELL_TILT_DEG = 45
# default guard_tilt_deg (recover starts lying, so any tilt guard would trip at once)
GUARD_DEG = {"walk": 40, "turn": 40, "balance": 30, "pose": 30, "recover": None, "generic": 30}
STEP_M = 0.05  # assumed stride when there is no phone (only for ranking walk candidates against each other)
YAW_NOISE_DEG = 7.0  # conservative end of the documented 3--7 degree gyro uncertainty


def measured(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and isfinite(value)


def _number(value, default=0.0):
    return float(value) if measured(value) else default


def fell(job: dict | None, after: dict | None) -> bool:
    """Lying (tonyd's tilt > 60) or tilted past 45 deg once the move ended."""
    after, job = after or {}, job or {}
    tilts = (after.get("tilt_deg"), job.get("end_tilt_deg"))
    return bool(after.get("lying") or job.get("lying_after")
                or any(measured(t) and t > FELL_TILT_DEG for t in tilts))


def temp_rise(before: dict | None, after: dict | None) -> float | None:
    """Largest servo temperature rise (deg C) between two fresh states."""
    b, a = (before or {}).get("temps") or {}, (after or {}).get("temps") or {}
    rises = [a[k] - b[k] for k in a if k in b and a[k] is not None and b[k] is not None]
    return max(rises) if rises else None


def score(objective: str, job: dict, before: dict | None = None, after: dict | None = None,
          episode: dict | None = None, steps: int = 1, *, turn_direction: str | None = None,
          turn_metric: str = "angle") -> dict:
    if objective not in OBJECTIVES:
        raise ValueError(f"objective is one of {', '.join(OBJECTIVES)}")
    job = job or {}
    f = fell(job, after)
    done = job.get("state") == "done"
    tilt = _number(job.get("max_tilt_deg"))
    yaw = job.get("yaw_deg")
    secs = _number(job.get("seconds"))
    end_tilt = (after or {}).get("tilt_deg", job.get("end_tilt_deg"))
    tracked = (episode or {}).get("frames_tracked")
    raw_disp = (episode or {}).get("target_moved_m")
    disp = raw_disp if measured(tracked) and tracked >= 2 and measured(raw_disp) and raw_disp >= 0 else None
    m = {"objective": objective, "state": job.get("state"), "fell": f, "max_tilt_deg": job.get("max_tilt_deg"),
         "yaw_deg": yaw, "end_tilt_deg": end_tilt, "seconds": job.get("seconds"), "dT_max": temp_rise(before, after),
         "disp_m": disp, "steps": steps}
    penalty = 2.0 * f
    if objective == "walk":
        if disp is not None:
            speed, m["basis"] = disp / secs if secs > 0 else 0.0, "phone"
        else:
            speed, m["basis"] = (steps * STEP_M if done and not f else 0.0) / secs if secs > 0 else 0.0, "steps"
        m["forward_mps"] = round(speed, 4)
        s = speed - 0.02 * abs(_number(yaw)) - 0.5 * (tilt > 25) - penalty
    elif objective == "turn":
        m["yaw_per_step"] = round(abs(yaw) / max(1, steps), 2) if measured(yaw) else None
        m["yaw_rate_dps"] = yaw / secs if measured(yaw) and secs > 0 else None
        sign = {"left": 1, "right": -1}.get(turn_direction)
        m["turn_direction"], m["turn_metric"] = turn_direction, turn_metric
        directed = _number(yaw) * sign if sign else abs(_number(yaw))
        value = directed / secs if turn_metric == "rate" and secs > 0 else directed / max(1, steps)
        m["basis"] = "phone" if disp is not None else "steps"
        s = value - 3.0 * (disp or 0.0) - 0.1 * max(0.0, tilt - 20) - penalty
    elif objective == "balance":
        m["held_s"] = secs if done and not f else 0.0
        m["basis"] = "steps"
        s = m["held_s"] - 0.05 * tilt - penalty
    elif objective == "recover":
        m["basis"] = "steps"
        m["recovered"] = done and not f
        # Goal evidence is stricter than the historical ranking/recovered fields; unknowns never prove recovery.
        duration = job.get("seconds")

        m["recovery_goal_eligible"] = (
            done and not f and (before or {}).get("lying") is True and (after or {}).get("lying") is False
            and measured(end_tilt) and 0 <= end_tilt < 10 and measured(duration) and duration >= 0
        )
        s = -_number(end_tilt) / 10 - 0.05 * secs - (not done) - penalty
    elif objective == "pose":
        m["basis"] = "steps"
        s = -_number(end_tilt) / 10 - (not done) - penalty
    else:
        m["basis"] = "steps"
        s = -tilt / 10 - (not done) - penalty
    m["score"] = round(s, 4)
    reasons = []
    if not done:
        reasons.append("job did not finish done")
    if f:
        reasons.append("trial fell")
    for key in ("max_tilt_deg", "seconds"):
        if not measured(job.get(key)) or job[key] < 0 or (key == "seconds" and job[key] == 0):
            reasons.append(f"missing or invalid {key}")
    if not measured(end_tilt) or end_tilt < 0 or (after or {}).get("lying") is not False:
        reasons.append("missing or invalid final posture")
    if "end_tilt_deg" in job and not measured(job["end_tilt_deg"]):
        reasons.append("invalid job end_tilt_deg")
    if not isinstance((before or {}).get("lying"), bool) or not measured((before or {}).get("tilt_deg")):
        reasons.append("missing starting posture")
    if objective in ("walk", "turn") and not measured(yaw):
        reasons.append("missing or invalid yaw_deg")
    if objective == "walk" and disp is None:
        reasons.append("assumed step distance is ranking only")
    if raw_disp is not None and disp is None:
        reasons.append("invalid phone displacement evidence")
    if objective == "turn":
        if turn_direction not in ("left", "right"):
            reasons.append("turn direction is unspecified: absolute yaw is ranking only")
        elif measured(yaw) and yaw * {"left": 1, "right": -1}[turn_direction] <= 0:
            reasons.append("yaw is not in the requested direction")
        noise = YAW_NOISE_DEG * steps / secs if turn_metric == "rate" and secs > 0 else YAW_NOISE_DEG
        m["comparison_noise"] = noise + (0.06 if disp is not None else 0)
    else:
        # Tilt precision and phone displacement also impose a floor on a ranking score.
        m["comparison_noise"] = {"walk": 0.02 / secs + 0.02 * YAW_NOISE_DEG * steps if secs > 0 else 1.0,
                                 "balance": 0.15, "recover": 0.3, "pose": 0.3, "generic": 0.3}[objective]
    m["comparison_eligible"], m["comparison_reasons"] = not reasons, reasons
    return m


def stats(values: list[float]) -> dict:
    n = len(values)
    if not n:
        return {"n": 0, "mean": None, "std": None}
    mean = sum(values) / n
    std = (sum((v - mean) ** 2 for v in values) / (n - 1)) ** 0.5 if n > 1 else 0.0
    return {"n": n, "mean": round(mean, 4), "std": round(std, 4)}
