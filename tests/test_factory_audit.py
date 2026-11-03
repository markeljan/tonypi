"""Manufacturer audits use synthetic assets and simulator fakes; never use hardware."""

import importlib.util
import json
from pathlib import Path
import sqlite3

import pytest

SPEC = importlib.util.spec_from_file_location("factory_audit", Path(__file__).parents[1] / "tools/audit_factory_motions.py")
audit = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(audit)

CAL = {"trim": {1: 10}, "limits": {1: [0, 1000]}}
HARDWARE = {"1": {"limits": [0, 1000]}}


def d6a(path, pulse=510, extra=17):
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as db:
        db.execute(f'CREATE TABLE ActionGroup ("Index" INTEGER, Time INTEGER, Servo1 INTEGER, Servo{extra} INTEGER)')
        db.execute("INSERT INTO ActionGroup VALUES (1, 100, ?, 500)", (pulse,))


def local(path, pulse=500, ms=100):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"source": "manufacturer", "frames": [{"ms": ms, "pos": {"1": pulse}}]}))


def test_all_sources_alias_exact_content_and_trim_only_once(tmp_path, monkeypatch):
    factory, pab, official = [tmp_path / name for name in ("factory", "pab", "official")]
    local(factory / "local.json")
    d6a(pab / "action" / "different_name.d6a")
    d6a(official / "ActionGroups" / "official.d6a")
    monkeypatch.setattr(audit.catalog, "revision", lambda _: audit.OFFICIAL_REVISION)
    rows, variants = audit.inventory(factory, pab, official, CAL, HARDWARE)
    assert len(rows) == 3 and len(variants) == 1
    assert {r["source"] for r in rows} == {"installed", "pab", "official"}
    assert all(r["hardware_format_supported"] and r["eeprom_limits_verified"] for r in rows)
    assert rows[1]["ignored_channels"] == [17]
    assert rows[0]["asset_sha256"] != rows[1]["asset_sha256"]
    variant = next(iter(variants.values()))
    assert variant["frames"] == [{"ms": 100, "pos": {1: 500}}]
    assert rows[1]["original_body_sha256"] != rows[1]["frames_sha256"]


def test_bad_asset_does_not_abort_and_limits_are_separate(tmp_path):
    factory, pab = tmp_path / "factory", tmp_path / "pab"
    local(factory / "short.json", ms=10)
    (factory / "broken.json").write_text("invalid")
    d6a(pab / "action" / "bad_channel.d6a", extra=19)
    (pab / "action" / "corrupt.d6a").write_text("not sqlite")
    rows, variants = audit.inventory(factory, pab, None, CAL, {"1": {"limits": [600, 900]}})
    assert len(rows) == 4 and len(variants) == 1
    assert sum("error" in r for r in rows) == 3
    row = next(r for r in rows if r["name"] == "short")
    assert not row["hardware_format_supported"] and not row["eeprom_limits_verified"]
    assert row["eeprom_limit_error_count"] == 1


def test_software_clipping_is_never_simulated(tmp_path):
    local(tmp_path / "factory" / "clip.json", pulse=995)
    rows, variants = audit.inventory(tmp_path / "factory", tmp_path / "pab", None, CAL, HARDWARE)
    assert "clip" in rows[0]["error"] and not variants


def test_recovery_is_only_named_factory_recovery(tmp_path):
    for name in ("stand_up_front", "stand_up_back", "get_up", "wave"):
        local(tmp_path / "factory" / (name + ".json"))
    _, variants = audit.inventory(tmp_path / "factory", tmp_path / "pab", None, CAL, HARDWARE)
    assert {(v["start"], v["objective"]) for v in variants.values()} == {
        ("home", "stability"), ("prone", "recover"), ("supine", "recover")}


def artifacts(directory, request):
    directory.mkdir(parents=True)
    physics = {"simulation_success": True, "model_joint_clips": [], "trace": []}
    report = dict(physics, request_digest=audit.catalog.digest(request), revision=request["revision"],
                  count=1, calibration_source="tonyd", soft_limit_clips=[], hardware_executed=False, id="test")
    for name, value in (("request", request), ("report", report), ("physics", physics)):
        (directory / (name + ".json")).write_text(json.dumps(value))
    return report


def request():
    return {"revision": "revision", "adapter": "adapter", "start": "home", "objective": "stability",
            "count": 1, "settle_s": 1.0, "initial_raw": None, "render": False,
            "plan": {"count": 1, "calibration_source": "tonyd", "soft_limit_clips": [],
                     "calibration": CAL, "hardware_frames": [{"ms": 100, "pos": {1: 500}}]}}


@pytest.mark.parametrize("change", ["digest", "physics", "incomplete", "count", "calibration_source"])
def test_corrupt_or_incomplete_report_never_reused(tmp_path, change):
    directory = tmp_path / "reports" / "one"
    report = artifacts(directory, request())
    assert len(audit.report_cache(tmp_path / "reports")) == 1
    if change == "incomplete":
        (directory / "physics.json").unlink()
    else:
        if change == "digest":
            report["request_digest"] = "wrong"
        elif change == "physics":
            report["simulation_success"] = False
        elif change == "count":
            report["count"] = 2
        else:
            report["calibration_source"] = "identity"
        (directory / "report.json").write_text(json.dumps(report))
    assert audit.report_cache(tmp_path / "reports") == {}


@pytest.mark.parametrize("field", ["revision", "adapter", "start", "objective", "count", "settle_s",
                                   "initial_raw", "render", "calibration", "hardware_frames"])
def test_cache_key_binds_all_request_conditions(tmp_path, field):
    original = request()
    artifacts(tmp_path / "reports" / "one", original)
    cache = audit.report_cache(tmp_path / "reports")
    modified = json.loads(json.dumps(original))
    target = modified["plan"] if field in ("calibration", "hardware_frames") else modified
    target[field] = "changed"
    assert audit.catalog.digest(modified) not in cache


class ReadOnlyClient:
    def call(self, method, **kwargs):
        assert method in ("calibration", "servo_calibration")
        return {"calibration": CAL} if method == "calibration" else {"servos": HARDWARE}


def test_audit_resumes_from_exact_reports_without_payload_output(tmp_path, monkeypatch):
    factory, repo, reports = [tmp_path / name for name in ("factory", "repo", "reports")]
    local(factory / "a.json")
    local(factory / "alias.json")
    output = tmp_path / "audit.json"
    monkeypatch.setattr(audit.catalog, "verify", lambda _: audit.catalog.REVISION)
    monkeypatch.setattr(audit.service, "adapter_digest", lambda: "adapter")
    calls = []

    def simulate(client, **kwargs):
        calls.append(kwargs)
        variant = {"frames": kwargs["frames"], "start": kwargs["start"], "objective": kwargs["objective"]}
        expected = audit.expected_request(client, repo, variant)
        return artifacts(reports / "one", expected)

    monkeypatch.setattr(audit.service, "simulate", simulate)
    for _ in range(2):
        audit.audit(ReadOnlyClient(), factory, repo, None, output, reports)
    assert len(calls) == 1
    data = json.loads(output.read_text())
    assert data["counts"]["inventoried"] == 2
    assert data["counts"]["model_verified"] == 1
    assert data["counts"]["physically_verified"] == 0
    assert next(iter(data["variants"].values()))["reused"]
    assert '"frames":' not in output.read_text()
    assert '"pos":' not in output.read_text()


def test_simulation_error_keeps_other_results_and_inventory_only_is_model_free(tmp_path, monkeypatch):
    factory = tmp_path / "factory"
    local(factory / "first.json")
    local(factory / "second.json", pulse=600)
    monkeypatch.setattr(audit.catalog, "verify", lambda _: audit.catalog.REVISION)
    monkeypatch.setattr(audit.service, "adapter_digest", lambda: "adapter")
    calls = []

    def fail(*args, **kwargs):
        calls.append(1)
        raise ValueError("worker failed")

    monkeypatch.setattr(audit.service, "simulate", fail)
    args = (ReadOnlyClient(), factory, tmp_path / "repo", None, tmp_path / "audit.json", tmp_path / "reports")
    audit.audit(*args, inventory_only=True)
    assert calls == []
    audit.audit(*args)
    assert len(calls) == 2
    result = json.loads((tmp_path / "audit.json").read_text())
    assert all(v["simulation_error"] == "worker failed" for v in result["variants"].values())
