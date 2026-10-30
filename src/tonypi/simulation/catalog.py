"""Read the pinned PAB47 assets and resolve Tony's motions without touching servos."""

from __future__ import annotations

import ast
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess

URL = "https://github.com/pab47/TonyPi"
REVISION = "97807f5e26049ce1c3d90b1c80d5d2a92606d40f"
MAX_FRAMES = 2000
MAX_SECONDS = 120


def root() -> Path:
    return Path(os.environ.get("TONYPI_SIM_DIR", Path.home() / ".local/share/tonypi/simulation"))


def repository() -> Path:
    return root() / "PAB47-TonyPi"


def digest(value) -> str:
    # Normalize integer servo keys before sorting, so saved/reloaded JSON hashes identically.
    value = json.loads(json.dumps(value))
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def revision(repo: Path) -> str:
    return subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True,
                                   stderr=subprocess.DEVNULL, timeout=10).strip()


def verify(repo: Path) -> str:
    if revision(repo) != REVISION:
        raise ValueError(f"expected PAB47/TonyPi revision {REVISION}; run tonypi-sim install")
    # The adapter depends on this exact calibration, model and action format. Refuse edited assets.
    p = subprocess.run(["git", "-C", str(repo), "status", "--porcelain", "--untracked-files=all", "--",
                        "model", "action", "control/servo_map.py"], capture_output=True, text=True, timeout=10,
                       check=True)
    if p.stdout.strip():
        raise ValueError("upstream simulation assets have local changes; use a clean pinned checkout")
    return REVISION


def mapping(repo: Path) -> tuple[dict[int, int], dict[int, int]]:
    """Only parse literal calibration tables; never execute upstream code."""
    values = {}
    for node in ast.parse((repo / "control/servo_map.py").read_text()).body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in ("ZERO", "SIGN"):
                    values[target.id] = ast.literal_eval(node.value)
    zero, sign = values["ZERO"], values["SIGN"]
    if set(zero) != set(range(1, 17)) or set(sign) != set(zero) or any(s not in (-1, 1) for s in sign.values()):
        raise ValueError("unsupported upstream servo mapping")
    return zero, sign


def frames_checked(frames: list[dict]) -> list[dict]:
    if not isinstance(frames, list) or not 1 <= len(frames) <= MAX_FRAMES:
        raise ValueError(f"need 1..{MAX_FRAMES} keyframes")
    out = []
    for f in frames:
        ms = f["ms"]
        if type(ms) is not int or not 1 <= ms <= 10000:
            raise ValueError("frame ms must be an integer in 1..10000 (hardware requires >=20)")
        pos = {int(k): v for k, v in f["pos"].items()}
        if not pos or any(k not in range(1, 17) or type(v) is not int or not 0 <= v <= 1000
                          for k, v in pos.items()):
            raise ValueError("frame positions must use body servos 1..16 and integer pulses 0..1000")
        out.append({"ms": ms, "pos": pos})
    if sum(f["ms"] for f in out) > MAX_SECONDS * 1000:
        raise ValueError(f"motion exceeds {MAX_SECONDS} simulated seconds")
    return out


def d6a(path: Path) -> tuple[list[dict], list[int]]:
    """Immutable read-only SQLite input, fixed identifiers, bounded rows."""
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 10_000_000:
        raise ValueError("action must be a regular .d6a file smaller than 10 MB")
    with sqlite3.connect(path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True) as db:
        cols = [r[1] for r in db.execute("PRAGMA table_info(ActionGroup)")]
        if "Index" not in cols or "Time" not in cols:
            raise ValueError(".d6a requires ActionGroup(Index, Time, ServoN...)")
        servos = [c for c in cols if re.fullmatch(r"Servo[1-9][0-9]?", c)]
        if not servos:
            raise ValueError("no servo columns in ActionGroup")
        query = 'SELECT "Time",' + ",".join(f'"{c}"' for c in servos) + ' FROM ActionGroup ORDER BY "Index" LIMIT ?'
        frames, ignored = [], set()
        for row in db.execute(query, (MAX_FRAMES + 1,)):
            pos = {}
            for col, value in zip(servos, row[1:]):
                sid = int(col[5:])
                if sid > 16:
                    ignored.add(sid)
                elif value is not None:
                    pos[sid] = value
            frames.append({"ms": row[0], "pos": pos})
    return frames_checked(frames), sorted(ignored)


def calibration(client) -> dict:
    cal = client.call("calibration")["calibration"]
    return {"trim": {int(k): int(v) for k, v in cal.get("trim", {}).items()},
            "limits": {int(k): list(v) for k, v in cal.get("limits", {}).items()}}


def local_frames(client, name: str) -> tuple[list[dict], list[int]]:
    """Match tonyd's body parser: factory head channels 17/18 are ignored."""
    frames, ignored = [], set()
    for frame in client.call("motion_get", name=name)["frames"]:
        positions = {int(k): v for k, v in frame["pos"].items()}
        ignored.update(k for k in positions if k in (17, 18))
        frames.append({"ms": frame["ms"], "pos": {k: v for k, v in positions.items() if k not in (17, 18)}})
    return frames_checked(frames), sorted(ignored)


def calibrated(frames: list[dict], cal: dict, raw: bool) -> tuple[list[dict], list[dict], list[dict]]:
    """Produce actual raw targets and tonyd motion-space targets, reporting every soft-limit clamp."""
    pulses, motion, clips = [], [], []
    for n, frame in enumerate(frames):
        rp, mp = {}, {}
        for sid, pos in frame["pos"].items():
            trim = cal["trim"].get(sid, 0)
            requested = pos if raw else pos + trim
            lo, hi = cal["limits"].get(sid, (0, 1000))
            actual = max(0, lo, min(1000, hi, requested))
            if actual != requested:
                clips.append({"frame": n, "servo": sid, "requested": requested, "actual": actual})
            rp[sid], mp[sid] = actual, actual - trim if raw else pos
        pulses.append({"ms": frame["ms"], "pos": rp})
        motion.append({"ms": frame["ms"], "pos": mp})
    return pulses, motion, clips


def resolve(repo: Path, client, motion: str = "", frames: list[dict] | None = None, count: int = 1) -> dict:
    if type(count) is not int or not 1 <= count <= 5:
        raise ValueError("count must be 1..5")
    if bool(motion) == (frames is not None):
        raise ValueError("give exactly one of motion or frames")
    ignored, skill, raw = [], None, False
    if frames is not None:
        resolved = frames_checked(frames) * count
        source = "frames"
    else:
        kind, sep, name = motion.partition(":")
        if not sep or not re.fullmatch(r"[\w-]+", name):
            raise ValueError("motion is upstream:NAME, motion:NAME or skill:NAME (see simulation_catalog)")
        source = kind
        if kind == "upstream":
            resolved, ignored = d6a(repo / "action" / f"{name}.d6a")
            resolved, raw = resolved * count, True
        elif kind == "motion":
            if client is None:
                raise ValueError("tonyd is required for local motion/skill discovery")
            resolved, ignored = local_frames(client, name)
            resolved *= count
        elif kind == "skill":
            if client is None:
                raise ValueError("tonyd is required for local motion/skill discovery")
            details = client.call("skills")["details"]
            if name not in details or not details[name].get("motions"):
                raise ValueError("scripted/dynamic skill has no faithful keyframe simulation")
            resolved, skill = [], name
            for entry in details[name]["motions"]:
                repeated = entry.startswith("*")
                fs, omitted = local_frames(client, entry.lstrip("*"))
                ignored = sorted(set(ignored) | set(omitted))
                resolved.extend(fs * (count if repeated else 1))
        else:
            raise ValueError("unknown motion namespace")
    resolved = frames_checked(resolved)
    # Upstream raw counts are independent of Tony's trim, but still obey Tony's soft limits.
    # Offline upstream simulation can use identity calibration when tonyd is unavailable.
    if client is None:
        if source not in ("upstream", "frames"):
            raise ValueError("tonyd is required for local motion/skill discovery")
        cal, cal_source = {"trim": {}, "limits": {}}, "identity (tonyd unavailable)"
    else:
        cal, cal_source = calibration(client), "tonyd"
    pulses, hardware, clips = calibrated(resolved, cal, raw)
    hardware_ok = (len(hardware) <= 400 and all(f["ms"] >= 20 and all(0 <= p <= 1000 for p in f["pos"].values())
                                             for f in hardware))
    return {"motion": motion, "source": source, "count": count, "input_frames": frames,
            "raw_frames": pulses, "hardware_frames": hardware, "hardware_skill": skill,
            "hardware_format_supported": hardware_ok, "calibration": cal, "calibration_source": cal_source,
            "soft_limit_clips": clips, "ignored_channels": ignored}


def catalog(repo: Path, client=None) -> dict:
    upstream = []
    if repo.is_dir():
        for p in sorted((repo / "action").glob("*.d6a")):
            row = {"id": f"upstream:{p.stem}", "source": "PAB47/TonyPi"}
            try:
                fs, ignored = d6a(p)
                row.update(simulation_supported=True, frames=len(fs), seconds=sum(f["ms"] for f in fs) / 1000,
                           ignored_channels=ignored,
                           hardware_format_supported=len(fs) <= 400 and all(f["ms"] >= 20 for f in fs))
            except (ValueError, sqlite3.Error, KeyError) as e:
                row.update(simulation_supported=False, reason=str(e))
            upstream.append(row)
    local, errors = [], []
    if client is not None:
        try:
            for row in client.call("motions")["motions"]:
                local.append({"id": f"motion:{row['name']}", "source": row.get("source"),
                              "simulation_supported": True})
            for name, row in client.call("skills")["details"].items():
                supported = bool(row.get("motions"))
                local.append({"id": f"skill:{name}", "description": row.get("description"),
                              "simulation_supported": supported,
                              "reason": None if supported else "scripted/runtime behaviour; use existing run_skill"})
        except (OSError, RuntimeError) as e:
            errors.append(str(e))
    return {"repository": URL, "expected_revision": REVISION, "upstream": upstream, "local": local,
            "errors": errors, "hardware_execution": "separate simulation_execute call; never automatic"}
