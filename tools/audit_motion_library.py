#!/usr/bin/env python3
"""Rebuild the curated manifest using read-only calibration and model-only experiments. Never drives hardware."""

import argparse
import json
from pathlib import Path

from tonypi.motion.catalog import check_frames, frame_digest
from tonypi.motion.library import DATA_DIR
from tonypi.simulation import catalog, service
from tonypi.tonyd.client import TonydClient

SEQUENCES = {
    "wave": ["wave"], "bow": ["bow"], "kick_left": ["left_shot_fast"], "kick_right": ["right_shot_fast"],
    "squat": ["squat_down", "squat_up"], "arms_up": ["both_arms_up"], "flap_wings": ["wings_up"],
    "step_in_place": ["*stepping"], "walk_forward": ["*go_forward_one_step"],
    "walk_backward": ["back_start", "*back", "back_end"], "turn_left": ["*turn_left_small_step_a"],
    "turn_right": ["*turn_right_small_step_a"], "step_left": ["*left_move"], "step_right": ["*right_move"],
    "stand": ["stand_slow"], "lie_down": ["lie_down"],
    "get_up_front": ["stand_up_front", "stand_slow"], "get_up_back": ["stand_up_back", "stand_slow"],
}


def compact(report):
    keys = ("id", "simulation_success", "max_tilt_deg", "end_tilt_deg", "model_joint_clip_count",
            "soft_limit_clip_count", "revision", "calibration_source", "report_path")
    return {k: report[k] for k in keys}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--source-repo", type=Path, help="read-only official Hiwonder/TonyPi checkout")
    p.add_argument("--output", type=Path, default=DATA_DIR.parent / "motion_catalog.json")
    p.add_argument("--motion-reports", type=Path, help="reuse existing reports from this exact audit")
    args = p.parse_args()
    if args.source_repo and catalog.revision(args.source_repo) != "dc452eaac9991516600860f706f4b889f3305d1d":
        raise ValueError("official source checkout revision differs; review source/license metadata first")
    client = TonydClient()
    if client.state().get("busy"):
        raise ValueError("robot busy; defer read-only EEPROM calibration audit")
    hardware = client.call("servo_calibration", ids=list(range(1, 17)))["servos"]
    calibration = catalog.calibration(client)
    motions, skills = {}, {}
    cached = {r["name"]: r for r in json.loads(args.motion_reports.read_text())} if args.motion_reports else {}
    for path in sorted(DATA_DIR.glob("*.json")):
        name = path.stem
        raw = json.loads(path.read_text())
        frames = [{"ms": f.ms, "pos": f.pos} for f in check_frames(raw["frames"], calibration)]
        raw_frames, _, _ = catalog.calibrated(frames, calibration, raw=False)
        for frame in raw_frames:
            for sid, pulse in frame["pos"].items():
                lo, hi = hardware[str(sid)]["limits"]
                if not lo <= pulse <= hi:
                    raise ValueError(f"{name} exceeds servo {sid} EEPROM limits")
        start = "prone" if name == "stand_up_front" else "supine" if name == "stand_up_back" else "home"
        report = cached.get(name)
        if report is not None:
            request_path = Path(report["report_path"]).parent / "request.json"
            request = json.loads(request_path.read_text())
            if (frame_digest(request["plan"]["hardware_frames"]) != frame_digest(frames)
                    or catalog.digest(request["plan"]["calibration"]) != catalog.digest(calibration)
                    or request["adapter"] != service.adapter_digest()):
                raise ValueError(f"cached simulation differs from current motion/calibration/adapter: {name}")
        if report is None:
            report = service.simulate(client, frames=frames, start=start,
                                      objective="recover" if start != "home" else "stability", render=False)
        original = args.source_repo / "ActionGroups" / f"{name}.d6a" if args.source_repo else None
        source = {"url": "https://github.com/Hiwonder/TonyPi", "revision": "dc452eaac9991516600860f706f4b889f3305d1d",
                  "license": "NOASSERTION", "license_note": "No LICENSE or COPYING in official repository tree; manufacturer image assets retained, no new redistribution grant inferred."}
        if original and original.exists():
            upstream, ignored = catalog.d6a(original)
            source.update(path=f"ActionGroups/{name}.d6a", frames_match_official=frames == upstream,
                          ignored_channels=ignored)
        else:
            source.update(path=raw.get("source"), frames_match_official=None)
        motions[name] = {"frames_sha256": frame_digest(raw["frames"]), "duration_ms": sum(f["ms"] for f in frames),
                         "source": source, "kinematic_verified": True, "simulation": compact(report),
                         "model_verified": bool(report.get("simulation_success") and not report.get("model_joint_clip_count")
                                                and not report.get("soft_limit_clip_count")), "hardware_verified": False}
        print("motion", name, motions[name]["model_verified"], flush=True)
    for name, entries in SEQUENCES.items():
        reports = {}
        for count in (range(1, 6) if any(e.startswith("*") for e in entries) else (1,)):
            frames = []
            for entry in entries:
                raw = json.loads((DATA_DIR / f"{entry.lstrip('*')}.json").read_text())
                frames.extend([{"ms": f.ms, "pos": f.pos} for f in check_frames(raw["frames"], calibration)]
                              * (count if entry.startswith("*") else 1))
            recovery = name.startswith("get_up_")
            report = service.simulate(client, frames=frames,
                                      start="prone" if name == "get_up_front" else "supine" if recovery else "home",
                                      objective="recover" if recovery else "stability", render=False)
            reports[str(count)] = compact(report)
        verified = all(r["simulation_success"] and not r["model_joint_clip_count"]
                       and not r["soft_limit_clip_count"] for r in reports.values())
        skills[name] = {"description": name.replace("_", " "), "motions": entries,
                        "precondition": "lying_front" if name == "get_up_front" else "lying_back" if name == "get_up_back" else "standing",
                        "duration_ms": sum(motions[e.lstrip('*')]["duration_ms"] for e in entries),
                        "safety": "Tonyd reflexes and servo limits apply; stable floor; physical acceptance pending.",
                        "model_verified": verified, "hardware_verified": False, "simulation_counts": reports,
                        "availability_reason": "model checked; owner hardware acceptance pending" if verified else "model stability or joint clipping check failed; learning mode only"}
        print("skill", name, verified, flush=True)
    args.output.write_text(json.dumps({"schema_version": 1, "calibration": calibration,
                                     "hardware_calibration_snapshot": hardware,
                                     "verification_scope": "Model and kinematic checks only; no physical execution or hardware success claimed.",
                                     "simulator": {"url": catalog.URL, "revision": catalog.REVISION, "license": "MIT"},
                                     "motions": motions, "skills": skills}, indent=2) + "\n")


if __name__ == "__main__":
    main()
