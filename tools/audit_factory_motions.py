#!/usr/bin/env python3
"""Inventory manufacturer actions and audit exact variants in simulation. Never executes hardware."""

import argparse
import hashlib
import json
import sqlite3
from pathlib import Path

from tonypi.motion.catalog import check_frames
from tonypi.motion.library import DATA_DIR
from tonypi.simulation import catalog, service
from tonypi.tonyd.client import TonydClient

OFFICIAL_REVISION = "dc452eaac9991516600860f706f4b889f3305d1d"
OFFICIAL_URL = "https://github.com/Hiwonder/TonyPi"


def inventory(factory_dir, pab_repo, official_repo, calibration, hardware):
    """Keep payloads in memory; output rows contain hashes and provenance, never manufacturer frames."""
    sources = [("installed", factory_dir, "*.json", None, None)]
    sources.append(("pab", pab_repo / "action", "*.d6a", catalog.URL, catalog.REVISION))
    if official_repo:
        if catalog.revision(official_repo) != OFFICIAL_REVISION:
            raise ValueError("official checkout is not the reviewed manufacturer revision")
        sources.append(("official", official_repo / "ActionGroups", "*.d6a", OFFICIAL_URL, OFFICIAL_REVISION))
    rows, variants = [], {}
    for source, directory, pattern, url, revision in sources:
        for path in sorted(directory.rglob(pattern)):
            row = {"name": path.stem, "source": source, "path": str(path), "source_url": url,
                   "source_revision": revision, "hardware_verified": False}
            rows.append(row)
            try:
                row["asset_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
                if source == "installed":
                    document = json.loads(path.read_text())
                    original = document["frames"]
                    row["import_source"] = document.get("source")
                    ignored = sorted({int(k) for f in original for k in f["pos"] if int(k) > 16})
                    frames = catalog.frames_checked([
                        {"ms": f["ms"], "pos": {int(k): v for k, v in f["pos"].items() if int(k) <= 16}}
                        for f in original])
                else:
                    frames, ignored = catalog.d6a(path)
                row.update(ignored_channels=ignored, original_body_sha256=catalog.digest(frames),
                           frame_count=len(frames), duration_ms=sum(f["ms"] for f in frames))
                if any(s not in (17, 18) for s in ignored):
                    raise ValueError("unsupported original channels outside body and omitted grippers 17/18")
                raw, motion, clips = catalog.calibrated(frames, calibration, raw=source != "installed")
                if clips:
                    raise ValueError("current software calibration would clip exact source contents")
                row["frames_sha256"] = catalog.digest(motion)
                row["raw_frames_sha256"] = catalog.digest(raw)
                try:
                    check_frames(motion, calibration)
                    row["hardware_format_supported"] = True
                except ValueError as exc:
                    row.update(hardware_format_supported=False, hardware_format_error=str(exc))
                eeprom_errors = []
                for index, frame in enumerate(raw):
                    for sid, pulse in frame["pos"].items():
                        limits = hardware.get(str(sid), hardware.get(sid, {})).get("limits")
                        if not limits or not limits[0] <= pulse <= limits[1]:
                            eeprom_errors.append({"frame": index, "servo": sid, "pulse": pulse,
                                                  "limits": limits})
                row["eeprom_limits_verified"] = not eeprom_errors
                row["eeprom_limit_error_count"] = len(eeprom_errors)
                row["eeprom_limit_errors_first_10"] = eeprom_errors[:10]
                start = {"stand_up_front": "prone", "stand_up_back": "supine"}.get(path.stem, "home")
                objective = "stability" if start == "home" else "recover"
                key = catalog.digest({"frames": motion, "start": start, "objective": objective})
                row["variant"] = key
                if key not in variants:
                    variants[key] = {"frames": motion, "start": start, "objective": objective,
                                     "frames_sha256": row["frames_sha256"], "assets": []}
                variants[key]["assets"].append(len(rows) - 1)
            except (OSError, ValueError, KeyError, TypeError, sqlite3.DatabaseError) as exc:
                row["error"] = str(exc)
    return rows, variants


def expected_request(client, repo, variant):
    return {"repo": str(repo), "revision": catalog.REVISION, "adapter": service.adapter_digest(),
            "plan": catalog.resolve(repo, client, frames=variant["frames"], count=1),
            "start": variant["start"], "initial_raw": None, "objective": variant["objective"],
            "settle_s": 1.0, "render": False}


def report_cache(reports_dir):
    """Only complete, internally consistent artifacts are cache entries; matching is full-request exact."""
    cache = {}
    for path in sorted(reports_dir.glob("*/report.json")):
        try:
            request = json.loads((path.parent / "request.json").read_text())
            report = json.loads(path.read_text())
            physics = json.loads((path.parent / "physics.json").read_text())
            digest = catalog.digest(request)
            plan = request["plan"]
            if (report["request_digest"] != digest or report["revision"] != request["revision"]
                    or report["count"] != plan["count"] or report["hardware_executed"] is not False
                    or report["calibration_source"] != plan["calibration_source"]
                    or report["soft_limit_clips"] != plan["soft_limit_clips"]
                    or type(report["simulation_success"]) is not bool
                    or not isinstance(report["model_joint_clips"], list)
                    or any(report.get(k) != v for k, v in physics.items())
                    or "trace" not in physics or "simulation_success" not in physics):
                continue
            cache[digest] = report
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return cache


def compact(report):
    keys = ("id", "simulation_success", "revision", "calibration_source", "report_path",
            "request_digest", "max_tilt_deg", "end_tilt_deg", "hardware_candidate")
    out = {k: report[k] for k in keys if k in report}
    for key in ("model_joint", "soft_limit"):
        out[key + "_clip_count"] = report.get(key + "_clip_count", len(report.get(key + "_clips", [])))
    out["model_verified"] = bool(out.get("simulation_success") and not out["model_joint_clip_count"]
                                 and not out["soft_limit_clip_count"])
    return out


def write_output(output, rows, variants, calibration, hardware):
    clean = {key: {k: v for k, v in value.items() if k != "frames"} for key, value in variants.items()}
    data = {"schema_version": 1, "scope": "model only; no hardware execution or promotion",
            "calibration": calibration, "hardware_calibration_snapshot": hardware, "assets": rows,
            "variants": clean, "counts": {
                "inventoried": len(rows), "unique_body_contents": len({r.get("frames_sha256") for r in rows
                                                                         if r.get("frames_sha256")}),
                "simulation_cases": len(variants), "asset_errors": sum("error" in r for r in rows),
                "simulated": sum("simulation" in v for v in variants.values()),
                "model_verified": sum(v.get("simulation", {}).get("model_verified", False)
                                      for v in variants.values()), "physically_verified": 0, "newly_approved": 0}}
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_text(json.dumps(data, indent=2, allow_nan=False) + "\n")
    temporary.replace(output)


def audit(client, factory_dir, repo, official_repo, output, reports_dir, inventory_only=False):
    catalog.verify(repo)
    calibration = catalog.calibration(client)
    hardware = client.call("servo_calibration", ids=list(range(1, 17)))["servos"]
    rows, variants = inventory(factory_dir, repo, official_repo, calibration, hardware)
    write_output(output, rows, variants, calibration, hardware)
    if inventory_only:
        return
    cache = report_cache(reports_dir)
    for key, variant in variants.items():
        try:
            request = expected_request(client, repo, variant)
            if catalog.digest(request["plan"]["calibration"]) != catalog.digest(calibration):
                raise ValueError("read-only calibration changed during audit; rerun inventory")
            cached = cache.get(catalog.digest(request))
            report = cached or service.simulate(client, frames=variant["frames"], start=variant["start"],
                                               objective=variant["objective"], render=False, repo=repo)
            if report.get("request_digest") != catalog.digest(request):
                raise ValueError("simulation report does not match exact requested contents/calibration")
            variant["simulation"] = compact(report)
            variant["reused"] = cached is not None
        except (OSError, ValueError, KeyError, TypeError) as exc:
            variant["simulation_error"] = str(exc)
        write_output(output, rows, variants, calibration, hardware)
        print(key[:12], variant.get("simulation", {}).get("model_verified", False),
              variant.get("simulation_error", ""), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--factory-dir", type=Path, default=DATA_DIR)
    parser.add_argument("--pab-repo", type=Path, default=catalog.repository())
    parser.add_argument("--official-repo", type=Path)
    parser.add_argument("--reports-dir", type=Path, default=catalog.root() / "reports")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--inventory-only", action="store_true")
    args = parser.parse_args()
    project = Path(__file__).resolve().parent.parent
    if args.output.resolve().is_relative_to(project):
        parser.error("audit metadata must be written outside the project repository")
    audit(TonydClient(), args.factory_dir, args.pab_repo, args.official_repo, args.output,
          args.reports_dir, args.inventory_only)


if __name__ == "__main__":
    main()
