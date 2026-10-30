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

OBJECTIVES = ("walk", "turn", "balance", "pose", "recover", "generic")
FELL_TILT_DEG = 45
# default guard_tilt_deg (recover starts lying, so any tilt guard would trip at once)
GUARD_DEG = {"walk": 40, "turn": 40, "balance": 30, "pose": 30, "recover": None, "generic": 30}
STEP_M = 0.05  # assumed stride when there is no phone (only for ranking walk candidates against each other)


def fell(job: dict | None, after: dict | None) -> bool:
    """Lying (tonyd's tilt > 60) or tilted past 45 deg once the move ended."""
    after, job = after or {}, job or {}
    tilt = after.get("tilt_deg", job.get("end_tilt_deg"))
    return bool(after.get("lying") or job.get("lying_after") or (tilt is not None and tilt > FELL_TILT_DEG))


def temp_rise(before: dict | None, after: dict | None) -> float | None:
    """Largest servo temperature rise (deg C) between two fresh states."""
    b, a = (before or {}).get("temps") or {}, (after or {}).get("temps") or {}
    rises = [a[k] - b[k] for k in a if k in b and a[k] is not None and b[k] is not None]
    return max(rises) if rises else None


def score(objective: str, job: dict, before: dict | None = None, after: dict | None = None,
          episode: dict | None = None, steps: int = 1) -> dict:
    if objective not in OBJECTIVES:
        raise ValueError(f"objective is one of {', '.join(OBJECTIVES)}")
    job = job or {}
    f = fell(job, after)
    done = job.get("state") == "done"
    tilt = float(job.get("max_tilt_deg") or 0.0)
    yaw = job.get("yaw_deg")
    secs = float(job.get("seconds") or 0.0)
    end_tilt = (after or {}).get("tilt_deg", job.get("end_tilt_deg"))
    disp = (episode or {}).get("target_moved_m") if (episode or {}).get("frames_tracked", 0) >= 2 else None
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
        s = speed - 0.02 * abs(yaw or 0) - 0.5 * (tilt > 25) - penalty
    elif objective == "turn":
        m["yaw_per_step"] = None if yaw is None else round(abs(yaw) / max(1, steps), 2)
        m["basis"] = "phone" if disp is not None else "steps"
        s = (m["yaw_per_step"] or 0.0) - 3.0 * (disp or 0.0) - 0.1 * max(0.0, tilt - 20) - penalty
    elif objective == "balance":
        m["held_s"] = secs if done and not f else 0.0
        m["basis"] = "steps"
        s = m["held_s"] - 0.05 * tilt - penalty
    elif objective == "recover":
        m["basis"] = "steps"
        m["recovered"] = done and not f
        s = -float(end_tilt or 0.0) / 10 - 0.05 * secs - (not done) - penalty
    elif objective == "pose":
        m["basis"] = "steps"
        s = -float(end_tilt or 0.0) / 10 - (not done) - penalty
    else:
        m["basis"] = "steps"
        s = -tilt / 10 - (not done) - penalty
    m["score"] = round(s, 4)
    return m


def stats(values: list[float]) -> dict:
    n = len(values)
    if not n:
        return {"n": 0, "mean": None, "std": None}
    mean = sum(values) / n
    std = (sum((v - mean) ** 2 for v in values) / (n - 1)) ** 0.5 if n > 1 else 0.0
    return {"n": n, "mean": round(mean, 4), "std": round(std, 4)}
