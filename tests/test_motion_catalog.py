"""Curated library validation uses only files and a fake manifest, never hardware."""

import json

import pytest

from tonypi.motion.catalog import check_frames, frame_digest, resolve_skill


@pytest.fixture
def curated(tmp_path):
    frames = [{"ms": 100, "pos": {"1": 500, "17": 500}}]
    (tmp_path / "stand.json").write_text(json.dumps({"frames": frames}))
    data = {"calibration": {"trim": {}, "limits": {}},
            "motions": {"stand": {"frames_sha256": frame_digest(frames)}},
            "skills": {"walk": {"model_verified": True, "motions": ["stand", "*stand"],
                                "availability_reason": "model only"},
                       "failed": {"model_verified": False, "availability_reason": "simulation failed"}}}
    manifest = tmp_path / "catalog.json"
    manifest.write_text(json.dumps(data))
    return tmp_path, manifest


def test_verified_sequence_repeats_only_marked_entries(curated):
    directory, manifest = curated
    motions = resolve_skill("walk", defaults=directory, count=3, manifest_path=manifest)
    assert len(motions) == 4
    assert motions[0].frames[0].pos == {1: 500}


@pytest.mark.parametrize("count", [True, 0, 6, 1.2])
def test_invalid_count(curated, count):
    with pytest.raises(ValueError, match="count"):
        resolve_skill("walk", count=count, manifest_path=curated[1])


def test_unknown_and_model_failure(curated):
    with pytest.raises(ValueError, match="unknown"):
        resolve_skill("../../secrets", manifest_path=curated[1])
    with pytest.raises(ValueError, match="simulation failed"):
        resolve_skill("failed", manifest_path=curated[1])


def test_hash_drift(curated):
    directory, manifest = curated
    (directory / "stand.json").write_text(json.dumps({"frames": [{"ms": 100, "pos": {"1": 600}}]}))
    with pytest.raises(ValueError, match="changed"):
        resolve_skill("walk", defaults=directory, manifest_path=manifest)


def test_current_limits_and_calibration_drift(curated):
    frames = [{"ms": 100, "pos": {"1": 500}}]
    with pytest.raises(ValueError, match="limits"):
        check_frames(frames, {"trim": {1: 20}, "limits": {1: [0, 510]}})
    with pytest.raises(ValueError, match="calibration changed"):
        resolve_skill("walk", calibration={"trim": {1: 1}}, manifest_path=curated[1])


def test_user_overrides_not_promoted(curated):
    directory, manifest = curated
    (directory / "skills.json").write_text(json.dumps({"walk": {"motions": ["stand"]}}))
    with pytest.raises(ValueError, match="unpromoted user skill"):
        resolve_skill("walk", defaults=directory, user_dir=directory, manifest_path=manifest)
    (directory / "skills.json").unlink()
    (directory / "motions").mkdir()
    (directory / "motions/stand.json").write_text("{}")
    with pytest.raises(ValueError, match="unpromoted user motion"):
        resolve_skill("walk", defaults=directory, user_dir=directory, manifest_path=manifest)


@pytest.mark.parametrize("frames", [[{"ms": 19, "pos": {"1": 500}}],
                                    [{"ms": 100, "pos": {"19": 500}}],
                                    [{"ms": 100, "pos": {"1": True}}]])
def test_invalid_hardware_frames(frames):
    with pytest.raises(ValueError):
        check_frames(frames)


def test_repository_manifest_hashes_and_verification():
    from tonypi.motion.catalog import manifest
    from tonypi.motion.library import DATA_DIR

    data = manifest()
    assert len(data["motions"]) == 83
    assert all(not m["hardware_verified"] for m in data["motions"].values())
    if not all((DATA_DIR / f"{name}.json").exists() for name in data["motions"]):
        pytest.skip("optional owner-supplied factory library not imported")
    for name, row in data["motions"].items():
        raw = json.loads((DATA_DIR / f"{name}.json").read_text())
        assert frame_digest(raw["frames"]) == row["frames_sha256"]
        check_frames(raw["frames"], data["calibration"])
    for name, skill in data["skills"].items():
        passed = all(r["simulation_success"] and r["model_joint_clip_count"] == 0
                     and r["soft_limit_clip_count"] == 0 for r in skill["simulation_counts"].values())
        assert skill["model_verified"] == passed
        if passed:
            for count in range(1, 6):
                assert resolve_skill(name, count=count)
