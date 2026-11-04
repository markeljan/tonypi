"""Single-trial provisional access, separate from repeated-trial hardware verification."""

from functools import wraps
import json
import math
from pathlib import Path
import re

from .catalog import check_frames, frame_digest, manifest
from .library import DATA_DIR

NOTE = ("Provisional: one or more guarded passes; repeated-trial reliability is unverified. "
        "Count one, synchronous guarded execution only; current joints must match the simulation.")


def _checked(fn):
    @wraps(fn)
    def checked(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except (KeyError, TypeError, AttributeError) as exc:
            raise ValueError("missing or malformed provisional evidence") from exc
    return checked


def _read(path: Path, default=None):
    try:
        value = json.loads(path.read_text()) if path.exists() else default
    except (OSError, ValueError) as exc:
        raise ValueError(f"cannot read provisional evidence {path.name}: {exc}") from exc
    if value is None or not isinstance(value, dict):
        raise ValueError(f"missing or malformed provisional evidence: {path.name}")
    return value


def _rows(path: Path) -> list[dict]:
    try:
        rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    except (OSError, ValueError) as exc:
        raise ValueError(f"cannot read physical evidence {path.name}: {exc}") from exc
    if any(not isinstance(row, dict) for row in rows):
        raise ValueError(f"malformed physical evidence: {path.name}")
    return rows


def _tilt(value, limit):
    return type(value) in (int, float) and math.isfinite(value) and 0 <= value <= limit


def _collision(name: str, user_dir: Path):
    if (name in manifest()["skills"] or name in _read(user_dir / "promoted_skills.json", {})
            or name in _read(user_dir / "skills.json", {})
            or (DATA_DIR / f"{name}.json").exists() or (user_dir / "motions" / f"{name}.json").exists()):
        raise ValueError("provisional name collides with an existing curated, default or promoted motion")


@_checked
def _evidence(report_dir: Path, ledger_path: Path, jobs_path: Path, calibration: dict) -> dict:
    from tonypi.learn.gait import digest
    from tonypi.simulation.catalog import digest as sim_digest
    from tonypi.simulation.service import adapter_digest

    request = _read(report_dir / "request.json")
    report = _read(report_dir / "report.json")
    plan = request.get("plan")
    if not isinstance(plan, dict) or not isinstance(plan.get("hardware_frames"), list):
        raise ValueError("missing or malformed simulation plan")
    if not re.fullmatch(r"[a-f0-9]{32}", report_dir.name):
        raise ValueError("invalid simulation report identity")
    label = f"simulation-{report_dir.name[:10]}"
    if (report.get("request_digest") != sim_digest(request) or request.get("adapter") != adapter_digest()
            or report.get("hardware_candidate") is not True or report.get("simulation_success") is not True
            or report.get("model_joint_clips") or report.get("soft_limit_clips") or plan.get("soft_limit_clips")
            or request.get("start") != "home" or request.get("objective") != "stability"
            or type(plan.get("count")) is not int or plan["count"] != 1):
        raise ValueError("provisional access needs unchanged unclipped upright count-one simulation")
    normalized = {k: {str(s): v for s, v in calibration.get(k, {}).items()} for k in ("trim", "limits")}
    if plan.get("calibration_source") != "tonyd" or plan.get("calibration") != normalized:
        raise ValueError("simulation calibration differs from current robot calibration")
    frames = plan["hardware_frames"]
    check_frames(frames, calibration)
    candidate, cal_digest = digest(frames), sim_digest(normalized)
    jobs = {}
    for job in _rows(jobs_path):
        key = job.get("id")
        if type(key) is not int:
            raise ValueError("malformed physical job identity")
        if key in jobs and jobs[key] != job:
            raise ValueError("contradictory duplicate physical job evidence")
        jobs[key] = job
    accepted = set()
    for row in _rows(ledger_path):
        if row.get("kind") != "trial" or row.get("cand") != candidate or row.get("count") != 1:
            continue
        job = jobs.get(row.get("job"), {})
        params = job.get("params") or {}
        if params.get("calibration_digest") not in (None, cal_digest):
            continue  # Evidence from a different calibration is never reused.
        if row.get("verdict") != "ok" or row.get("metrics", {}).get("fell") is not False:
            raise ValueError("failed or contradictory matching physical trial evidence")
        # Only the report's bridge trial establishes the simulated initial-joint preflight.
        if row.get("label") != label:
            continue
        if not (row.get("verdict") == "ok" and row.get("metrics", {}).get("fell") is False
                and row.get("guard_tilt_deg") == 30 and job.get("guard_tilt_deg") == 30
                and bool(row.get("exp")) and job.get("label") == row.get("exp")
                and job.get("state") == "done" and job.get("lying_after") is False
                and params.get("hash") == candidate and params.get("calibration_digest") == cal_digest
                and params.get("start_lying") is False and _tilt(params.get("start_tilt_deg"), 10)
                and row.get("before", {}).get("lying") is False
                and _tilt(row.get("before", {}).get("tilt_deg"), 10) and _tilt(job.get("end_tilt_deg"), 30)):
            raise ValueError("failed, incomplete or contradictory matching physical trial evidence")
        accepted.add(job["id"])
    for job in jobs.values():
        params = job.get("params") or {}
        if (params.get("hash") == candidate and params.get("calibration_digest") == cal_digest
                and job.get("guard_tilt_deg") == 30
                and (job.get("state") != "done" or job.get("lying_after") is not False
                     or not _tilt(job.get("end_tilt_deg"), 30))):
            raise ValueError("failed or incomplete matching physical job evidence")
    if not accepted:
        raise ValueError("provisional access requires one distinct matched guarded no-fall trial")
    return {"frames": frames, "frames_sha256": frame_digest(frames), "calibration": normalized,
            "request_digest": sim_digest(request), "report_digest": sim_digest(report),
            "simulation_report": str(report_dir), "source": plan.get("source"),
            "source_motion": plan.get("motion"), "simulation_revision": request.get("revision"),
            "physical_jobs": sorted(accepted), "physical_trial_count": len(accepted)}


def promote(name: str, report_dir: Path, ledger_path: Path, jobs_path: Path, user_dir: Path,
            calibration: dict, description: str = "") -> dict:
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", name):
        raise ValueError("skill name must be lowercase letters, digits and underscores")
    _collision(name, user_dir)
    evidence = _evidence(report_dir, ledger_path, jobs_path, calibration)
    path = user_dir / "provisional_skills.json"
    data = _read(path, {})
    if name in data:
        resolve(name, ledger_path, jobs_path, user_dir, calibration)
        if any(data[name].get(k) != evidence[k] for k in ("simulation_report", "request_digest", "frames_sha256")):
            raise ValueError("existing provisional identity cannot be replaced")
    value = {**evidence, "description": description[:300], "approval_level": "provisional",
             "model_verified": True, "hardware_verified": False, "reliability_verified": False,
             "count_limit": 1, "precondition": "standing", "locomotion_unknown": True,
             "availability_reason": NOTE}
    data[name] = value
    user_dir.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".json.tmp")
    temp.write_text(json.dumps(data, indent=2) + "\n")
    temp.replace(path)
    return {k: v for k, v in value.items() if k != "frames"}


@_checked
def resolve(name: str, ledger_path: Path, jobs_path: Path, user_dir: Path, calibration: dict) -> dict:
    _collision(name, user_dir)
    value = registry(user_dir).get(name)
    if not isinstance(value, dict):
        raise ValueError("unknown provisional skill")
    if not isinstance(value.get("simulation_report"), str):
        raise ValueError("missing or malformed provisional simulation report")
    evidence = _evidence(Path(value["simulation_report"]), ledger_path, jobs_path, calibration)
    fixed = {"approval_level": "provisional", "hardware_verified": False, "model_verified": True,
             "reliability_verified": False, "count_limit": 1, "precondition": "standing", "locomotion_unknown": True}
    if (any(value.get(k) != v for k, v in fixed.items())
            or any(value.get(k) != evidence[k] for k in ("frames", "frames_sha256", "calibration", "request_digest",
                                                         "report_digest", "source", "source_motion", "simulation_revision"))
            or not value.get("physical_jobs") or len(set(value["physical_jobs"])) != len(value["physical_jobs"])
            or value.get("physical_trial_count") != len(value["physical_jobs"])
            or not set(value["physical_jobs"]) <= set(evidence["physical_jobs"])):
        raise ValueError("provisional evidence changed; approval is unavailable")
    return {**value, **evidence}


def registry(user_dir: Path) -> dict:
    return _read(user_dir / "provisional_skills.json", {})


def library(ledger_path: Path, jobs_path: Path, user_dir: Path, calibration: dict) -> dict:
    result = {}
    for name, value in registry(user_dir).items():
        row = {k: v for k, v in value.items() if k != "frames"} if isinstance(value, dict) else {}
        try:
            current = resolve(name, ledger_path, jobs_path, user_dir, calibration)
            row.update({k: v for k, v in current.items() if k != "frames"})
            row["available"] = True
        except (ValueError, OSError, KeyError, TypeError) as exc:
            row.update(available=False, availability_reason=str(exc))
        result[name] = row
    return result


def execute(client, name: str, ledger_path: Path, jobs_path: Path, user_dir: Path, calibration: dict) -> dict:
    from tonypi.simulation.service import execute as execute_report

    value = resolve(name, ledger_path, jobs_path, user_dir, calibration)
    report_id = Path(value["simulation_report"]).name
    result = execute_report(client, report_id)  # Exactly one existing guarded bridge call; refusals propagate.
    return {**result, "approval_level": "provisional", "reliability_verified": False, "provisional_note": NOTE}
