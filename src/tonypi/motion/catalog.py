"""Curated, immutable factory skills. Model verification is separate from hardware acceptance."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .library import DATA_DIR, REPO_DATA_DIR, Frame, Motion, read_factory

MANIFEST = REPO_DATA_DIR / "motion_catalog.json"


def manifest(path: Path = MANIFEST) -> dict:
    return json.loads(path.read_text())


def skill_catalog(path: Path = MANIFEST, user_dir: Path | None = None, calibration: dict | None = None) -> dict:
    out = manifest(path)["skills"]
    if user_dir is not None:
        promoted = user_dir / "promoted_skills.json"
        if promoted.exists():
            out.update(json.loads(promoted.read_text()))
    for name, row in out.items():
        try:
            resolve_skill(name, calibration=calibration, user_dir=user_dir, manifest_path=path)
            row["available"] = True
        except ValueError as exc:
            row["available"] = False
            row["availability_reason"] = str(exc)
        row.pop("frames", None)  # catalog discovery never returns fine-control payloads
    return out


def frame_digest(frames: list[dict]) -> str:
    normalized = json.loads(json.dumps(frames))
    return hashlib.sha256(json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def check_frames(frames: list[dict], calibration: dict | None = None) -> list[Frame]:
    """Reject clipping instead of relying on playback to silently clamp a candidate."""
    from tonypi.simulation.catalog import calibrated, frames_checked

    if any(int(k) not in range(1, 19) for f in frames for k in f["pos"]):
        raise ValueError("only body channels 1..16 and omitted grippers 17/18 are supported")
    body = [{"ms": f["ms"], "pos": {int(k): v for k, v in f["pos"].items() if int(k) <= 16}} for f in frames]
    checked = frames_checked(body)
    if len(checked) > 400 or any(f["ms"] < 20 for f in checked):
        raise ValueError("hardware motions require <=400 frames and frame durations >=20 ms")
    calibration = calibration or {"trim": {}, "limits": {}}
    cal = {"trim": {int(k): v for k, v in calibration.get("trim", {}).items()},
           "limits": {int(k): v for k, v in calibration.get("limits", {}).items()}}
    _, _, clips = calibrated(checked, cal, raw=False)
    if clips:
        raise ValueError("motion exceeds current servo calibration limits")
    return [Frame(f["ms"], f["pos"]) for f in checked]


def resolve_skill(name: str, calibration: dict | None = None, defaults: Path = DATA_DIR,
                  count: int = 1, user_dir: Path | None = None, manifest_path: Path = MANIFEST) -> list[Motion]:
    """Return verified factory frames, never a user override or an arbitrary path/name."""
    if type(count) is not int or not 1 <= count <= 5:
        raise ValueError("count must be an integer in 1..5")
    data = manifest(manifest_path)
    current_cal = calibration or {"trim": {}, "limits": {}}
    normalized = {k: {str(s): v for s, v in current_cal.get(k, {}).items()} for k in ("trim", "limits")}
    if user_dir is not None:
        promoted_path = user_dir / "promoted_skills.json"
        promoted = json.loads(promoted_path.read_text()) if promoted_path.exists() else {}
        if name in promoted:
            row = promoted[name]
            if (not row.get("model_verified") or not row.get("hardware_verified")
                    or row.get("calibration") != normalized or count != 1
                    or len(set(row.get("physical_jobs", []))) < 5
                    or frame_digest(row["frames"]) != row["frames_sha256"]):
                raise ValueError("promoted skill evidence changed; validate and promote again")
            return [Motion(name, check_frames(row["frames"], calibration))]
    if normalized != data["calibration"]:
        raise ValueError("calibration changed since library validation; audit the library again")
    skill = data["skills"].get(name)
    if not skill:
        raise ValueError(f"unknown curated skill {name!r}")
    if not skill["model_verified"]:
        raise ValueError(f"skill {name!r} is not model verified: {skill['availability_reason']}")
    if user_dir is not None:
        skills_path = user_dir / "skills.json"
        if skills_path.exists() and name in json.loads(skills_path.read_text()):
            raise ValueError("unpromoted user skill overrides a curated name")
    motions = []
    for entry in skill["motions"]:
        motion_name = entry.lstrip("*")
        if user_dir is not None and (user_dir / "motions" / f"{motion_name}.json").exists():
            raise ValueError("unpromoted user motion overrides a curated motion")
        raw = read_factory(motion_name, defaults)
        if frame_digest(raw["frames"]) != data["motions"][motion_name]["frames_sha256"]:
            raise ValueError("curated motion changed; validate it before promotion")
        frames = check_frames(raw["frames"], calibration)
        motions.extend([Motion(motion_name, frames)] * (count if entry.startswith("*") else 1))
    return motions


def is_promoted(name: str, user_dir: Path) -> bool:
    path = user_dir / "promoted_skills.json"
    return path.exists() and name in json.loads(path.read_text())
