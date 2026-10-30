"""Bounded isolated simulation and explicit, separately authorized hardware trials."""

from __future__ import annotations

import fcntl
from importlib import metadata
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import uuid

from . import catalog as assets

LIMITATIONS = [
    "Model-only prediction, never evidence of real-world success or permission to move.",
    "Upstream TonyPi Pro geometry, masses, contact/friction, PD gains and ZERO/SIGN are not calibrated to Tony.",
    "Servo numbering follows upstream Python (1..8 left, 9..16 right); XML comments disagree about left/right.",
    "Head/gripper channels are not driven. Scripted/runtime skills and live IK controller generation are unsupported.",
    "Starting pose and floor are idealized; torque-off, backlash, heat, battery, obstacles and sensing errors are absent.",
    "Displacement and yaw are simulator coordinates, not phone measurements. No automatic hardware promotion.",
]


def status(repo: Path | None = None) -> dict:
    repo = repo or assets.repository()
    versions = {}
    for name in ("mujoco", "mink"):
        try:
            versions[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            versions[name] = None
    error = None
    try:
        rev = assets.verify(repo)
    except (OSError, ValueError, subprocess.SubprocessError) as e:
        error, rev = str(e), None
    return {"repository": assets.URL, "path": str(repo), "revision": rev, "expected_revision": assets.REVISION,
            "ready": rev is not None and versions["mujoco"] is not None, "error": error, "dependencies": versions,
            "install": "uv sync --extra agent --extra simulation; uv run tonypi-sim install",
            "limitations": LIMITATIONS}


def adapter_digest() -> str:
    return assets.digest({p.name: p.read_text() for p in Path(__file__).parent.glob("*.py")})


def simulate(client=None, motion: str = "", frames: list[dict] | None = None, count: int = 1,
             start: str = "home", initial_raw: dict | None = None, objective: str = "stability",
             settle_s: float = 1.0, render: bool = True, repo: Path | None = None) -> dict:
    repo = repo or assets.repository()
    assets.verify(repo)
    if start not in ("home", "prone", "supine") or objective not in ("stability", "recover"):
        raise ValueError("start: home|prone|supine; objective: stability|recover")
    if not math.isfinite(settle_s) or not 0 <= settle_s <= 5:
        raise ValueError("settle_s must be 0..5")
    if initial_raw is not None:
        initial_raw = {int(k): v for k, v in initial_raw.items()}
        if (start != "home" or set(initial_raw) != set(range(1, 17))
                or any(type(v) is not int or not 0 <= v <= 1000 for v in initial_raw.values())):
            raise ValueError("initial_raw requires home and all 16 raw integer servo pulses in 0..1000")
    plan = assets.resolve(repo, client, motion, frames, count)
    request = {"repo": str(repo), "revision": assets.REVISION, "adapter": adapter_digest(), "plan": plan,
               "start": start, "initial_raw": initial_raw, "objective": objective, "settle_s": settle_s,
               "render": render}
    reports = assets.root() / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    # This is a simulator resource lock, never the robot lock. Avoid parallel renderers using all Pi RAM.
    with (reports / ".simulation.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("another simulation is running; try again after it finishes") from None
        report_id = uuid.uuid4().hex
        directory = reports / report_id
        directory.mkdir()
        (directory / "request.json").write_text(json.dumps(request))
        env = dict(os.environ, MUJOCO_GL="osmesa", OPENBLAS_NUM_THREADS="1", OMP_NUM_THREADS="1")
        try:
            p = subprocess.run([sys.executable, "-m", "tonypi.simulation.engine", str(directory)],
                               env=env, capture_output=True, text=True, timeout=90)
        except subprocess.TimeoutExpired:
            raise ValueError(f"simulation exceeded 90 wall seconds; incomplete artifacts: {directory}") from None
        physics_path = directory / "physics.json"
        if p.returncode or not physics_path.is_file():
            reason = json.loads(physics_path.read_text()).get("error") if physics_path.is_file() else p.stderr[-1500:]
            raise ValueError(f"simulation worker failed: {reason}; artifacts: {directory}")
        physics = json.loads(physics_path.read_text())
        eligible = (physics["simulation_success"] and not physics["model_joint_clips"]
                    and not plan["soft_limit_clips"]
                    and plan["hardware_format_supported"] and plan["calibration_source"] == "tonyd"
                    and start == "home" and objective == "stability")
        report = {**physics, "id": report_id, "request_digest": assets.digest(request),
                  "motion": motion or "custom frames", "count": count, "revision": assets.REVISION,
                  "hardware_candidate": bool(eligible), "hardware_executed": False,
                  "scope": "body servos 1..16 only; other action channels are omitted in both backends",
                  "calibration_source": plan["calibration_source"], "ignored_channels": plan["ignored_channels"],
                  "soft_limit_clips": plan["soft_limit_clips"], "limitations": LIMITATIONS,
                  "artifacts": str(directory), "report_path": str(directory / "report.json")}
        (directory / "report.json").write_text(json.dumps(report, indent=1, allow_nan=False))
    # Full trace/clip lists stay in the report; tool output remains small even for long action files.
    return {k: v for k, v in report.items() if k not in ("trace", "model_joint_clips", "soft_limit_clips")} | {
        "trace_samples": len(report["trace"]), "model_joint_clip_count": len(report["model_joint_clips"]),
        "model_joint_clips_first_10": report["model_joint_clips"][:10],
        "soft_limit_clip_count": len(report["soft_limit_clips"])}


class TrialClient:
    """Delegate to tonyd, checking the simulated initial conditions just before the measured submission."""

    def __init__(self, client, plan, initial_raw):
        self.client, self.plan, self.initial_raw, self.checked = client, plan, initial_raw, False

    def __getattr__(self, name):
        return getattr(self.client, name)

    def check(self):
        st = self.client.state(fresh=True)
        if st.get("stop_latched"):
            raise ValueError("stop latch is on; no motion and no latch clearing")
        if st.get("busy") or st.get("lying") or st.get("tilt_deg") is None or st["tilt_deg"] > 10:
            raise ValueError("hardware trial requires an idle upright body with tilt <=10 deg")
        if st.get("battery_mv") is None or st["battery_mv"] < 10900:
            raise ValueError("battery must be measured and >=10.9 V")
        hottest = st.get("hottest")
        if not hottest or hottest[2] is None or hottest[2] >= 70:
            raise ValueError("servo temperature must be measured and below 70 C")
        if st.get("tether", {}).get("on", True):
            raise ValueError("simulation execution is refused while tethered: arbitrary actions lack reliable walking accounting")
        if assets.calibration(self.client) != self.plan["calibration"]:
            raise ValueError("calibration changed since simulation; simulate again")
        raw = {int(k): v for k, v in self.client.joints().get("raw", {}).items()}
        if (set(raw) != set(range(1, 17)) or any(raw[k] is None or abs(raw[k] - v) > 20
                                               for k, v in self.initial_raw.items())):
            raise ValueError("current raw joints differ from the simulated starting pose by >20 counts; simulate again")

    def run(self, skill, *args, **kwargs):
        if not self.checked:
            self.check()
            self.checked = True
        return self.client.run(skill, *args, **kwargs)

    def play(self, *args, **kwargs):
        self.check()
        self.checked = True
        return self.client.play(*args, **kwargs)


def execute(client, report_id: str) -> dict:
    """Only call with current owner authorization for body work; never from a simulation-only task."""
    if not re.fullmatch(r"[0-9a-f]{32}", report_id):
        raise ValueError("report_id must be a simulation report id")
    directory = assets.root() / "reports" / report_id
    request = json.loads((directory / "request.json").read_text())
    report = json.loads((directory / "report.json").read_text())
    if not report.get("hardware_candidate") or not report.get("simulation_success"):
        raise ValueError("report is not an unclipped, supported upright hardware candidate; inspect simulation results")
    if report["request_digest"] != assets.digest(request) or request["adapter"] != adapter_digest():
        raise ValueError("simulation inputs or adapter changed; simulate again")
    repo = assets.repository()
    assets.verify(repo)
    plan = request["plan"]
    current = assets.resolve(repo, client, plan["motion"], plan["input_frames"], plan["count"])
    if assets.digest(current) != assets.digest(plan):
        raise ValueError("motion or calibration changed since simulation; simulate again")
    guarded = TrialClient(client, current, {int(k): v for k, v in report["initial_raw"].items()})
    guarded.check()  # fail before preparing a trial; repeated immediately before the body submission
    from tonypi.learn.runner import Lab

    spec = {"objective": "balance", "guard_tilt_deg": 30, "start": "as_is", "trace": True,
            "label": f"simulation-{report_id[:10]}"}
    if current["hardware_skill"]:
        spec.update(skill=current["hardware_skill"], count=current["count"])
    else:
        spec["frames"] = current["hardware_frames"]
    result = Lab(tonyd=guarded).batch(spec, trials=1)
    return {"simulation_report": report_id, "physical_trial": result,
            "hardware_validated": bool(result["trials"] and result["trials"][0]["verdict"] == "ok"),
            "note": "One guarded physical trial; repeat measured trials before promoting a skill."}
