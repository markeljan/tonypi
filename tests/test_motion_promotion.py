import json

import pytest

from tonypi.learn.gait import digest
from tonypi.motion.catalog import frame_digest, resolve_skill
from tonypi.motion.promotion import promote
from tonypi.simulation.catalog import digest as simulation_digest
from tonypi.simulation.service import adapter_digest


@pytest.fixture
def evidence(tmp_path):
    frames = [{"ms": 100, "pos": {"1": 500}}]
    cal = {"trim": {}, "limits": {}}
    request = {"adapter": adapter_digest(), "start": "home", "objective": "stability",
               "plan": {"count": 1, "calibration_source": "tonyd", "calibration": cal, "hardware_frames": frames}}
    report = {"request_digest": simulation_digest(request), "simulation_success": True,
              "model_joint_clips": [], "soft_limit_clips": []}
    (tmp_path / "request.json").write_text(json.dumps(request))
    (tmp_path / "report.json").write_text(json.dumps(report))
    candidate = digest(frames)
    rows = [{"kind": "trial", "cand": candidate, "count": 1, "verdict": "ok", "metrics": {"fell": False}, "before": {"lying": False, "tilt_deg": 2}, "job": i}
            for i in range(5)]
    jobs = [{"id": i, "state": "done", "lying_after": False, "end_tilt_deg": 4, "params": {"hash": candidate, "calibration_digest": simulation_digest(cal), "start_lying": False, "start_tilt_deg": 2}}
            for i in range(5)]
    for file, data in (("ledger.jsonl", rows), ("jobs.jsonl", jobs)):
        (tmp_path / file).write_text("\n".join(json.dumps(r) for r in data))
    return tmp_path, frames, cal


def run_promotion(evidence):
    directory, _, cal = evidence
    return promote("hello", directory, directory / "ledger.jsonl", directory / "jobs.jsonl", directory, cal)


def test_exact_evidence_promotes(evidence):
    row = run_promotion(evidence)
    assert row["hardware_verified"] is True
    assert len(row["physical_jobs"]) == 5
    directory, frames, cal = evidence
    manifest = directory / "catalog.json"
    manifest.write_text(json.dumps({"calibration": cal, "skills": {}}))
    motions = resolve_skill("hello", calibration=cal, user_dir=directory, manifest_path=manifest)
    assert motions[0].frames[0].pos == {1: 500}
    assert row["frames_sha256"] == frame_digest(frames)
    with pytest.raises(ValueError, match="evidence changed"):
        resolve_skill("hello", count=2, calibration=cal, user_dir=directory, manifest_path=manifest)


@pytest.mark.parametrize("failure", ["simulation", "frames", "falls", "duplicate", "jobs", "calibration"])
def test_rejects_bad_evidence(evidence, failure):
    directory, _, cal = evidence
    if failure in ("simulation", "frames"):
        p = directory / ("report.json" if failure == "simulation" else "request.json")
        d = json.loads(p.read_text())
        if failure == "simulation":
            d["model_joint_clips"] = [{"servo": 1}]
        else:
            d["plan"]["hardware_frames"][0]["pos"]["1"] = 600
        p.write_text(json.dumps(d))
    elif failure in ("falls", "duplicate"):
        p = directory / "ledger.jsonl"
        rows = [json.loads(line) for line in p.read_text().splitlines()]
        if failure == "falls":
            rows[0]["metrics"]["fell"] = True
        else:
            rows[-1] = rows[0]
        p.write_text("\n".join(json.dumps(r) for r in rows))
    elif failure == "jobs":
        (directory / "jobs.jsonl").write_text("")
    else:
        cal["trim"]["1"] = 2
    with pytest.raises(ValueError):
        run_promotion(evidence)


@pytest.mark.parametrize('field,value', [('calibration_digest', 'stale'), ('start_tilt_deg', None),
                                        ('start_lying', True)])
def test_legacy_or_wrong_start_job_evidence_cannot_promote(evidence, field, value):
    directory, _, _ = evidence
    path = directory / 'jobs.jsonl'
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    rows[0]['params'][field] = value
    path.write_text('\n'.join(json.dumps(row) for row in rows))
    with pytest.raises(ValueError, match='five distinct'):
        run_promotion(evidence)
