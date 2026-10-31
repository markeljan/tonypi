"""Explicit promotion from immutable simulation inputs and measured physical trial records."""

import json
from pathlib import Path
import re

from .catalog import check_frames, frame_digest


def promote(name: str, report_dir: Path, ledger_path: Path, jobs_path: Path, user_dir: Path,
            calibration: dict, description: str = "") -> dict:
    from tonypi.learn.gait import digest
    from tonypi.simulation.catalog import digest as simulation_digest
    from tonypi.simulation.service import adapter_digest

    if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", name):
        raise ValueError("skill name must be lowercase letters, digits and underscores")
    request = json.loads((report_dir / "request.json").read_text())
    report = json.loads((report_dir / "report.json").read_text())
    if (report.get("request_digest") != simulation_digest(request) or request["adapter"] != adapter_digest()
            or not report.get("simulation_success") or report.get("model_joint_clips")
            or report.get("soft_limit_clips") or request["start"] != "home"
            or request["objective"] != "stability" or request["plan"]["count"] != 1):
        raise ValueError("promotion needs an unchanged, unclipped upright simulation for count 1")
    normalized = {k: {str(s): v for s, v in calibration.get(k, {}).items()} for k in ("trim", "limits")}
    if request["plan"]["calibration_source"] != "tonyd" or request["plan"]["calibration"] != normalized:
        raise ValueError("simulation calibration differs from current robot calibration")
    frames = request["plan"]["hardware_frames"]
    check_frames(frames, calibration)
    candidate = digest(frames)
    jobs = {row["id"]: row for line in jobs_path.read_text().splitlines() if line.strip()
            for row in [json.loads(line)]}
    accepted = set()
    for line in ledger_path.read_text().splitlines():
        row = json.loads(line)
        job = jobs.get(row.get("job"), {})
        if (row.get("kind") == "trial" and row.get("cand") == candidate and row.get("count") == 1
                and row.get("verdict") == "ok" and row.get("metrics", {}).get("fell") is False
                and job.get("state") == "done" and job.get("lying_after") is False
                and (job.get("params") or {}).get("hash") == candidate
                and (job.get("params") or {}).get("calibration_digest") == simulation_digest(normalized)
                and (job.get("params") or {}).get("start_lying") is False
                and (job.get("params") or {}).get("start_tilt_deg") is not None
                and job["params"]["start_tilt_deg"] <= 10
                and row.get("before", {}).get("lying") is False
                and row.get("before", {}).get("tilt_deg") is not None
                and row["before"]["tilt_deg"] <= 10
                and job.get("end_tilt_deg") is not None and job["end_tilt_deg"] <= 30):
            accepted.add(job["id"])
    if len(accepted) < 5:
        raise ValueError("promotion requires five distinct completed frame trials with matching hashes and zero falls")
    value = {"description": description[:300], "frames": frames, "frames_sha256": frame_digest(frames),
             "calibration": normalized, "model_verified": True, "hardware_verified": True,
             "count_limit": 1, "locomotion_unknown": True, "simulation_report": str(report_dir), "physical_jobs": sorted(accepted),
             "precondition": "standing", "availability_reason": "model checked and >=5 matched no-fall trials"}
    user_dir.mkdir(parents=True, exist_ok=True)
    path = user_dir / "promoted_skills.json"
    data = json.loads(path.read_text()) if path.exists() else {}
    data[name] = value
    temp = path.with_suffix(".json.tmp")
    temp.write_text(json.dumps(data, indent=2) + "\n")
    temp.replace(path)
    return value
