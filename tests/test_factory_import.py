"""Owner-supplied imports use synthetic fixtures, never vendor recordings or hardware."""
import importlib.util
import json
from pathlib import Path
import sqlite3

import pytest

from tonypi.motion.catalog import frame_digest, resolve_skill, skill_catalog
from tonypi.motion.library import load

spec = importlib.util.spec_from_file_location("factory_import", Path(__file__).parents[1] / "tools/import_factory_motions.py")
importer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(importer)


def setup(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    frames = [{"ms": 100, "pos": {"1": 510, "2": 490}}]
    manifest = tmp_path / "catalog.json"
    manifest.write_text(json.dumps({"calibration": {"trim": {}, "limits": {}},
        "motions": {"synthetic": {"frames_sha256": frame_digest(frames)}},
        "skills": {"test": {"model_verified": True, "motions": ["synthetic"]}}}))
    return source, tmp_path / "dest", manifest, frames


def test_d6a_round_trip_and_catalog(tmp_path):
    source, dest, manifest, frames = setup(tmp_path)
    with sqlite3.connect(source / "synthetic.d6a") as db:
        db.execute('CREATE TABLE ActionGroup ("Index" INTEGER, Time INTEGER, Servo1 INTEGER, Servo2 INTEGER)')
        db.execute("INSERT INTO ActionGroup VALUES (1, 100, 510, 490)")
    report = importer.import_actions(source, dest, manifest)
    assert report["validated_hash_matches"] == ["synthetic"]
    assert not report["missing"] and not report["different_requires_audit"]
    assert resolve_skill("test", defaults=dest, manifest_path=manifest)[0].frames[0].pos == {1: 510, 2: 490}
    assert importer.import_actions(source, dest, manifest) == report


def test_missing_and_changed_sources_fail_closed(tmp_path):
    source, dest, manifest, frames = setup(tmp_path)
    with pytest.raises(ValueError, match="import your own"):
        resolve_skill("test", defaults=dest, manifest_path=manifest)
    with pytest.raises(ValueError, match="import your own"):
        load("synthetic", dest)
    frames[0]["pos"]["1"] = 520
    (source / "synthetic.json").write_text(json.dumps({"frames": frames}))
    report = importer.import_actions(source, dest, manifest)
    assert report["different_requires_audit"] == ["synthetic"]
    with pytest.raises(ValueError, match="changed"):
        resolve_skill("test", defaults=dest, manifest_path=manifest)
    frames[0]["pos"]["1"] = 530
    (source / "synthetic.json").write_text(json.dumps({"frames": frames}))
    with pytest.raises(ValueError, match="existing local action differs"):
        importer.import_actions(source, dest, manifest)


def test_import_rejects_unignored_repository_destination(tmp_path, monkeypatch):
    source, dest, manifest, frames = setup(tmp_path)
    (source / "synthetic.json").write_text(json.dumps({"frames": frames}))
    monkeypatch.setattr(importer, "REPO_ROOT", tmp_path)
    with pytest.raises(ValueError, match="repository destinations"):
        importer.import_actions(source, dest, manifest)
    report = importer.import_actions(source, tmp_path / "factory-motions", manifest)
    assert report["validated_hash_matches"] == ["synthetic"]
