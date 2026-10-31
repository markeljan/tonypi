#!/usr/bin/env python3
"""Import owner-supplied factory actions locally. Never downloads or drives hardware."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile

DEFAULT_DEST = Path(os.environ.get("TONYPI_FACTORY_MOTIONS_DIR", Path.home() / ".local/share/tonypi/factory-motions"))
REPO_ROOT = Path(__file__).resolve().parents[1]
MANIFEST = REPO_ROOT / "data/motion_catalog.json"


def digest(frames: list[dict]) -> str:
    return hashlib.sha256(json.dumps(frames, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def read_action(path: Path) -> dict:
    if path.suffix == ".json":
        raw = json.loads(path.read_text())
    else:
        with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as db:
            columns = [r[1] for r in db.execute("PRAGMA table_info(ActionGroup)")]
            if len(columns) < 3 or columns[:2] != ["Index", "Time"] or any(
                    not re.fullmatch(r"Servo\d+", c) for c in columns[2:]):
                raise ValueError(f"{path.name}: unsupported ActionGroup schema")
            frames = [{"ms": r[1], "pos": {str(int(c[5:])): v for c, v in zip(columns[2:], r[2:])}}
                      for r in db.execute('SELECT * FROM ActionGroup ORDER BY "Index"')]
        raw = {"name": path.stem, "frames": frames}
    frames = raw["frames"]
    if not isinstance(frames, list) or not frames:
        raise ValueError(f"{path.name}: no frames")
    for f in frames:
        if type(f["ms"]) is not int or not 20 <= f["ms"] <= 10000 or not f["pos"]:
            raise ValueError(f"{path.name}: invalid duration or empty targets")
        for key, value in f["pos"].items():
            if not str(key).isdigit() or not 1 <= int(key) <= 18 or type(value) is not int or not 0 <= value <= 1000:
                raise ValueError(f"{path.name}: invalid servo target")
    return {"name": path.stem, "frames": frames,
            "source": str(path.resolve()), "license": "Owner-supplied manufacturer data; not project MIT"}


def import_actions(source: Path, dest: Path, manifest_path: Path = MANIFEST) -> dict:
    source, dest = source.resolve(), dest.resolve()
    if source == dest or not source.is_dir():
        raise ValueError("source must be a separate existing directory of your own .d6a or legacy JSON actions")
    if dest.is_relative_to(REPO_ROOT) and not any(dest.is_relative_to(allowed) for allowed in
            (REPO_ROOT / "factory-motions", REPO_ROOT / "data/motions")):
        raise ValueError("repository destinations must be ignored factory-motions/ or data/motions/")
    expected = json.loads(manifest_path.read_text())["motions"]
    candidates = {}
    for path in sorted(source.rglob("*")):
        if path.suffix in (".d6a", ".json") and path.stem in expected:
            # Conflicting copies must be resolved by the owner, never silently chosen.
            raw = read_action(path)
            if path.stem in candidates and digest(candidates[path.stem]["frames"]) != digest(raw["frames"]):
                raise ValueError(f"conflicting source copies: {path.stem}; select a narrower source directory")
            candidates[path.stem] = raw
    if not candidates:
        raise ValueError("no recognized factory action files found")
    # Validate everything before writing any file. Never replace an existing different local action.
    for name, raw in candidates.items():
        target = dest / f"{name}.json"
        if target.exists() and digest(json.loads(target.read_text())["frames"]) != digest(raw["frames"]):
            raise ValueError(f"existing local action differs: {name}; preserve it and choose another destination")
    dest.mkdir(parents=True, exist_ok=True)
    for name, raw in candidates.items():
        with tempfile.NamedTemporaryFile(mode="w", dir=dest, prefix=".import-", delete=False) as file:
            json.dump(raw, file, indent=1)
            file.write("\n")
            temporary = Path(file.name)
        temporary.replace(dest / f"{name}.json")
    matching = sorted(n for n, r in candidates.items() if digest(r["frames"]) == expected[n]["frames_sha256"])
    report = {"imported": len(candidates), "validated_hash_matches": matching,
              "different_requires_audit": sorted(set(candidates) - set(matching)),
              "missing": sorted(set(expected) - set(candidates)), "destination": str(dest)}
    (dest / "import-report.txt").write_text(json.dumps(report, indent=2) + "\n")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="your original SD/image ActionGroups directory or official download")
    parser.add_argument("--dest", type=Path, default=DEFAULT_DEST)
    args = parser.parse_args()
    try:
        report = import_actions(args.source, args.dest)
    except (ValueError, KeyError, sqlite3.Error) as exc:
        parser.exit(1, f"Import failed: {exc}\n")
    print(json.dumps(report, indent=2))
    if report["missing"] or report["different_requires_audit"]:
        parser.exit(2, "Import incomplete for the pinned catalog; missing/different actions remain unavailable. See docs/FACTORY_MOTIONS.md.\n")


if __name__ == "__main__":
    main()
