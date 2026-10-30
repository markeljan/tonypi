"""Pure keyframe transforms for searching over a base motion ({"frames": [{"ms": .., "pos": {"1": ..}}]}).

Candidates are a base motion plus a few parameters (time scale, amplitude, per-servo offsets, mirror), not
hand-edited keyframes, so a search moves along a handful of dimensions. Positions are clamped to 0..1000 and frame
times to tonyd's 20..10000 ms; the grippers (17/18) in factory files are dropped.
"""

from __future__ import annotations

import hashlib
import json
from functools import cache
from pathlib import Path

import yaml

ROBOT_YAML = Path(__file__).resolve().parents[1] / "config" / "robot.yaml"
SERVOS = range(1, 17)
MIN_MS, MAX_MS = 20, 10_000


@cache
def stand_pose() -> dict[int, int]:
    """The stand pose from config/robot.yaml (servo -> position)."""
    cfg = yaml.safe_load(ROBOT_YAML.read_text())
    return {int(k): int(v["stand"]) for k, v in cfg["bus_servos"].items() if int(k) in SERVOS}


@cache
def joint_names() -> dict[int, str]:
    cfg = yaml.safe_load(ROBOT_YAML.read_text())
    return {int(k): v["name"] for k, v in cfg["bus_servos"].items() if int(k) in SERVOS}


def partner(servo: int) -> int:
    """The same joint on the other side (L 1..8 <-> R 9..16)."""
    return servo + 8 if servo <= 8 else servo - 8


def frames(motion) -> list[dict]:
    """Normalised frames (int servo keys, 1..16 only) from a motion dict or a frame list."""
    raw = motion["frames"] if isinstance(motion, dict) else motion
    return [{"ms": int(f["ms"]), "pos": {int(k): int(v) for k, v in f["pos"].items() if int(k) in SERVOS}}
            for f in raw]


def _out(fr: list[dict]) -> dict:
    return {"frames": [{"ms": max(MIN_MS, min(MAX_MS, round(f["ms"]))),
                        "pos": {str(k): max(0, min(1000, round(v))) for k, v in sorted(f["pos"].items())}}
                       for f in fr if f["pos"]]}


def time_scale(motion, f: float) -> dict:
    """Every frame f times as long (f < 1 = faster)."""
    return _out([{**fr, "ms": fr["ms"] * f} for fr in frames(motion)])


def amplitude(motion, scale: float, servos=None) -> dict:
    """Every position's distance from the stand pose times scale (only `servos` if given): 1.2 = bigger steps."""
    st, only = stand_pose(), None if servos is None else {int(s) for s in servos}
    return _out([{**fr, "pos": {k: st[k] + (v - st[k]) * scale if only is None or k in only else v
                                for k, v in fr["pos"].items()}} for fr in frames(motion)])


def offset(motion, deltas: dict) -> dict:
    """Add a delta to a servo wherever a frame commands it, e.g. {"5": +15, "13": -15} = wider hip roll."""
    d = {int(k): int(v) for k, v in deltas.items()}
    return _out([{**fr, "pos": {k: v + d.get(k, 0) for k, v in fr["pos"].items()}} for fr in frames(motion)])


def mirror(motion) -> dict:
    """Left <-> right: each servo takes its partner's position reflected about 500 (exact for the factory
    turn_left -> turn_right; the stand pose is symmetric the same way)."""
    return _out([{**fr, "pos": {partner(k): 1000 - v for k, v in fr["pos"].items()}} for fr in frames(motion)])


def concat(*motions) -> dict:
    return _out([fr for m in motions for fr in frames(m)])


def repeat(motion, n: int) -> dict:
    return concat(*[motion] * max(1, int(n)))


def apply(motion, params: dict | None) -> dict:
    """One candidate from a base motion: params time_scale, amplitude (+ amplitude_servos), offset {servo: delta},
    mirror, repeat. Missing params leave the base as it is."""
    p = params or {}
    m = _out(frames(motion))
    if p.get("mirror"):
        m = mirror(m)
    if p.get("amplitude") not in (None, 1, 1.0):
        m = amplitude(m, float(p["amplitude"]), p.get("amplitude_servos"))
    if p.get("offset"):
        m = offset(m, p["offset"])
    if p.get("time_scale") not in (None, 1, 1.0):
        m = time_scale(m, float(p["time_scale"]))
    if int(p.get("repeat") or 1) > 1:
        m = repeat(m, int(p["repeat"]))
    return m


def digest(frames_or_motion) -> str:
    """Short stable hash of keyframes (same format tonyd puts in a job's params.hash)."""
    fr = frames(frames_or_motion)
    canon = json.dumps([[f["ms"], sorted(f["pos"].items())] for f in fr], separators=(",", ":"))
    return hashlib.sha1(canon.encode()).hexdigest()[:10]
