"""tonypi-mcp: the robot as an MCP server (Streamable HTTP), for Tony's Codex brain, Claude Code, or any MCP client.

A layer over tonyd's socket (body, camera) and the agent's socket (speech). It retains bounded camera history
but never touches the hardware, so it can restart freely. Default: http://127.0.0.1:8765/mcp
"""

from __future__ import annotations

import argparse
import json
import math
import logging
import os
import socket
from typing import Annotated, Literal

from pydantic import Field, StrictBool
from contextlib import asynccontextmanager, contextmanager

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.mcpserver.utilities.types import Image

from tonypi.tonyd.client import TonydClient, TonydError
from tonypi.video_context import FrameHistory

AGENT_SOCK = os.environ.get("TONYPI_SOCK", "/run/tonypi/agent.sock")
VOICE_SOCK = os.environ.get("TONYPI_VOICE_SOCK", "/run/tonypi/voice.sock")

context_camera = TonydClient()
frame_history = FrameHistory(lambda: context_camera.call("snapshot", timeout=2))


@asynccontextmanager
async def _lifespan(server):
    frame_history.start()
    try:
        yield {}
    finally:
        frame_history.stop()
        context_camera.close()


mcp = MCPServer(
    "tonypi",
    lifespan=_lifespan,
    instructions=(
        "Tony's everyday interface uses a curated motion library. Use status for available names, move for bounded "
        "steps, posture, get_up, gesture or run_skill for library composites. Unknown/unverified motions are refused. "
        "tonyd owns guarded fall recovery; stop never waits for a model. Do not invent joint angles. "
        "Learning tools require a separately launched owner-requested learning profile. Model validation is not "
        "hardware validation. Walking distance has no measured calibration; inspect after short moves."
    ),
)
tonyd = TonydClient()

PROFILE = os.environ.get("TONYPI_MCP_PROFILE", "everyday")
if PROFILE not in ("everyday", "learning"):
    raise ValueError("TONYPI_MCP_PROFILE must be everyday or learning")
StepCount = Annotated[int, Field(strict=True, ge=1, le=5)]
EVERYDAY_TOOLS = {"move", "posture", "get_up", "gesture", "look", "perceive", "say", "stop", "status", "run_skill"}


def _tool():
    def register(fn):
        if PROFILE == "learning" or fn.__name__ in EVERYDAY_TOOLS:
            mcp.tool()(fn)
        return fn
    return register


def _execute(skill: str, count: int, wait: bool) -> dict:
    if type(count) is not int or not 1 <= count <= 5:
        raise ToolError("steps/count must be an integer from 1 to 5")
    if type(wait) is not bool:
        raise ToolError("wait must be boolean")
    with _robot():
        return tonyd.call("execute", timeout=185 if wait else 10, skill=skill, count=count,
                          wait=wait, wait_s=180)["job"]


@_tool()
def move(direction: Literal["forward", "backward", "turn_left", "turn_right", "side_left", "side_right"], steps: StepCount = 1, wait: StrictBool = True) -> dict:
    """Move in 1..5 open-loop steps. direction: forward, backward, turn_left, turn_right, side_left, side_right.
    No distance conversion until measured on hardware; use status and perceive after a short leg."""
    names = {"forward": "walk_forward", "backward": "walk_backward", "turn_left": "turn_left",
             "turn_right": "turn_right", "side_left": "step_left", "side_right": "step_right"}
    if direction not in names:
        raise ToolError("unknown direction; use " + ", ".join(names))
    return _execute(names[direction], steps, wait)


@_tool()
def posture(name: Literal["stand", "squat", "rest"], wait: StrictBool = True) -> dict:
    """Library posture: stand, squat, rest (lie down). Names unavailable in the verified library are refused."""
    names = {"stand": "stand", "squat": "squat", "rest": "lie_down"}
    if name not in names:
        raise ToolError("unknown posture; use stand, squat, rest")
    return _execute(names[name], 1, wait)


@_tool()
def get_up(wait: StrictBool = True) -> dict:
    """Recover using fresh IMU front/back orientation and library motions. Never clears the stop latch."""
    return _execute("get_up", 1, wait)


@_tool()
def gesture(name: Literal["wave", "bow", "arms_up", "flap_wings", "kick_left", "kick_right"], wait: StrictBool = True) -> dict:
    """Run a named library gesture. status lists available gestures and verification limitations."""
    if name not in {"wave", "bow", "arms_up", "flap_wings", "kick_left", "kick_right"}:
        raise ToolError("unknown gesture; use wave, bow, arms_up, flap_wings, kick_left, kick_right")
    return _execute(name, 1, wait)


@_tool()
def status() -> dict:
    """Compressed posture, power, heat, stop latch, current/last job, and curated skill catalog. Does not move."""
    with _robot():
        state = tonyd.state(fresh=True)
        catalog = {name: {k: entry[k] for k in
                          ("description", "precondition", "available", "model_verified", "hardware_verified", "count_limit", "availability_reason")
                          if k in entry} for name, entry in tonyd.call("catalog")["skills"].items()}
        try:
            last = tonyd.job()
        except TonydError:
            last = None
    return {"profile": PROFILE, "state": state, "last_job": last, "skills": catalog}


@_tool()
def perceive(mode: Literal["snapshot", "context"] = "snapshot") -> list:
    """Observe without moving. snapshot returns the current camera image; context a compressed recent mosaic.
    Describe the returned image yourself; no automatic vision-model upload."""
    if mode == "context":
        return video_context(frames=6, seconds=6)
    if mode != "snapshot":
        raise ToolError("mode must be snapshot or context")
    return [snapshot()]


@contextmanager
def _robot():
    """Robot-side failures (unknown skill, no camera, tonyd down) go back to the model as readable errors."""
    try:
        yield
    except (TonydError, OSError) as e:
        raise ToolError(str(e)) from e


@_tool()
def list_skills() -> dict:
    """Every skill the body can run: description, source (builtin / default / user) and, for sequence skills,
    the motions it plays. count repeats steps for walking/turning skills."""
    with _robot():
        return tonyd.call("skills")["details"]


@_tool()
def run_skill(skill: str, count: StepCount = 1, wait: StrictBool = True) -> dict:
    """Run a curated skill or promoted composite; status lists available names. count: 1..5.
    Returns done/failed/cancelled; wait=False returns immediately. tonyd owns all everyday guards."""
    return _execute(skill, count, wait)


@_tool()
def practice_skill(skill: str, count: int = 1, wait: bool = True, guard_tilt_deg: float | None = None) -> dict:
    """LEARNING ONLY: run experimental skills/overrides with an optional measured-trial tilt guard."""
    with _robot():
        return tonyd.call("run", timeout=185 if wait else 10, skill=skill, count=count, wait=wait, wait_s=180,
                          guard_tilt_deg=guard_tilt_deg)["job"]


@_tool()
def wait_motion(timeout_s: float = 3.0, job_id: int | None = None) -> dict:
    """Wait up to timeout_s (max 3) for the running body job to finish. Use after run_skill(wait=False) in a loop
    for long moves: you stay steerable between the short waits. busy=false: `job` is the finished record (job_id's,
    else the last one: state, max_tilt_deg, yaw_deg, end_tilt_deg, lying_after, seconds); busy=true: the running job."""
    import time

    end = time.monotonic() + max(0.0, min(3.0, float(timeout_s)))
    with _robot():
        while True:
            st = tonyd.state()
            if not st["busy"] or time.monotonic() >= end:
                job = st["job"]
                if not st["busy"]:
                    try:
                        job = tonyd.job(job_id)
                    except TonydError:  # nothing finished yet since tonyd started
                        job = None
                return {"busy": st["busy"], "job": job, "tilt_deg": st["tilt_deg"], "lying": st["lying"],
                        "heading_deg": st.get("heading_deg"), "stop_latched": st.get("stop_latched")}
            time.sleep(0.2)


@_tool()
def get_job(job_id: int | None = None) -> dict:
    """A body job's record by id (default: the last finished one; tonyd keeps 200, all are in
    ~/.local/share/tonypi/jobs.jsonl): skill, state, said, seconds, t_start/t_end (epoch), max_tilt_deg, yaw_deg,
    rot_deg, end_tilt_deg, lying_after, hottest_before/after [servo, joint, C], battery_mv, label, trace path."""
    with _robot():
        return tonyd.job(job_id)


@_tool()
def stop() -> dict:
    """Stop whatever the body is doing, immediately."""
    with _robot():
        tonyd.stop(latch=True)
    return {"stopped": True}


@_tool()
def get_state() -> dict:
    """Posture (tilt, lying, asleep), battery mV, servo temperatures (degrees C), current skill, head angle,
    camera status."""
    with _robot():
        return tonyd.state(fresh=True)


@_tool()
def look(pan_deg: float = 0.0, tilt_deg: float = 0.0) -> dict:
    """Point the head (and camera). pan_deg: + left / - right, -60..60. tilt_deg: + up / - down, -40..25.
    0, 0 is straight ahead."""
    if (not math.isfinite(pan_deg) or not math.isfinite(tilt_deg)
            or not -60 <= pan_deg <= 60 or not -40 <= tilt_deg <= 25):
        raise ToolError("head angles: pan -60..60, tilt -40..25, finite degrees")
    with _robot():
        return tonyd.head(pan_deg, tilt_deg)


@_tool()
def snapshot() -> Image:
    """What Tony sees right now: a 640x480 JPEG from the head camera (from the camera's latest frame)."""
    with _robot():
        path = tonyd.snapshot()["path"]
        with open(path, "rb") as f:
            return Image(data=f.read(), format="jpeg")


def _images(shots: list[dict], label) -> list:
    out = []
    for shot in shots:
        out.append(label(shot))
        with open(shot["path"], "rb") as f:
            out.append(Image(data=f.read(), format="jpeg"))
    return out


@_tool()
def scan(pans: list[float] | None = None, tilt_deg: float | None = None) -> list:
    """Look around without moving the body: one photo per head pan angle (default 60, 0, -60; + = left, max 7
    angles, -60..60), each labelled with its angle, then the head returns. Use it to find things outside the
    current view before turning or walking."""
    with _robot():
        r = tonyd.call("scan", timeout=30, pans=pans or [60, 0, -60], tilt_deg=tilt_deg)
    return [f"heading {r['heading_deg']} deg"] + _images(
        r["views"], lambda v: f"head pan {v['pan_deg']:+.0f} deg, tilt {v['tilt_deg']:+.0f} deg")


@_tool()
def video(seconds: float = 2.0, frames: int = 4) -> list:
    """A short clip: `frames` photos (2-12) spread over `seconds` (0.2-10), oldest first, each with its time.
    To watch a move, start it with run_skill(..., wait=False) or play_frames first, then call this."""
    with _robot():
        r = tonyd.call("burst", timeout=float(seconds) + 15, frames=frames, seconds=seconds)
    return _images(r["frames"], lambda f: f"t = {f['t_s']:.1f} s")


@_tool()
def video_context(frames: int = 24, seconds: float = 12.0, end_ago_s: float = 0.0) -> list:
    """Past head-camera views in ONE timestamped mosaic, oldest first (row-major). Always buffered locally
    while MCP runs: 2 fps, 320x240, last 60 seconds, max 120 frames/8 MiB; no archive or automatic model uploads.
    Select 1-24 evenly spaced frames from a window of `seconds` ending `end_ago_s` before now (sum <=60).
    One frame returns the latest in that window. Startup/gaps may return fewer or none; metadata reports ages,
    estimated capture UTC and head angles. Timestamps include encoding/socket uncertainty. This is sampled
    visual context, not continuous model vision or a fast tracking controller. Does not move head/body."""
    try:
        status, jpeg = frame_history.mosaic(frames, seconds, end_ago_s)
    except (ValueError, OSError) as exc:
        raise ToolError(str(exc)) from exc
    return [json.dumps(status)] + ([Image(data=jpeg, format="jpeg")] if jpeg is not None else [])


@_tool()
def get_joints() -> dict:
    """Current bus servo positions: `pos` in motion space (what motions use) and `raw` servo counts."""
    with _robot():
        return tonyd.joints()


@_tool()
def play_frames(frames: list[dict], hold_s: float = 0, guard_tilt_deg: float | None = None, wait: bool = True) -> dict:
    """Play your own keyframes without saving them: [{"ms": 400, "pos": {"4": 620, "12": 380}}, ...]. Servos left
    out of a frame keep their position. hold_s > 0 keeps torque on (holding the last pose) that long afterwards,
    otherwise Tony goes limp in the last pose 1.5 s after. Returns the job, with max_tilt_deg."""
    with _robot():
        return tonyd.call("play", timeout=185 if wait else 10, frames=frames, hold_s=hold_s,
                          guard_tilt_deg=guard_tilt_deg, wait=wait, wait_s=180)["job"]


@_tool()
def set_pose(positions: dict[str, int], ms: int = 500, hold_s: float = 10) -> dict:
    """Move some servos to positions over `ms` and hold for hold_s (max 120). For calibrating and trying poses."""
    with _robot():
        return tonyd.pose(positions, ms=ms, hold_s=hold_s)


@_tool()
def set_torque(on: bool, ids: list[int] | None = None, hold_s: float = 0) -> dict:
    """Torque off (limp: pose Tony by hand, then get_joints to record it) or on for the given servos (default
    all 16). On is held for hold_s, then the idle rule switches servos off again."""
    with _robot():
        tonyd.torque(ids, on, hold_s=hold_s)
    return {"torque": on}


@_tool()
def list_motions() -> list[dict]:
    """Every motion (keyframe file): defaults from the factory library, and saved user motions (which override a
    default of the same name)."""
    with _robot():
        return tonyd.call("motions")["motions"]


@_tool()
def get_motion(name: str) -> dict:
    """A motion's keyframes, to study or copy before writing your own version."""
    with _robot():
        return tonyd.call("motion_get", name=name)


@_tool()
def save_motion(name: str, frames: list[dict], description: str = "") -> dict:
    """Save keyframes as a named motion (a default's name overrides it; delete_motion restores the default)."""
    with _robot():
        return tonyd.call("motion_save", name=name, frames=frames, description=description)


@_tool()
def delete_motion(name: str) -> dict:
    """Delete a user motion (restores the default of that name, if any)."""
    with _robot():
        return tonyd.call("motion_delete", name=name)


@_tool()
def save_skill(name: str, motions: list[str], description: str = "") -> dict:
    """Define a skill as a motion sequence; "*name" repeats with count. E.g. a better walk:
    save_skill("walk_forward", ["my_walk_start", "*my_step", "my_walk_end"])."""
    with _robot():
        return tonyd.call("skill_save", name=name, motions=motions, description=description)


@_tool()
def delete_skill(name: str) -> dict:
    """Delete a user skill (restores the default skill of that name, if any)."""
    with _robot():
        return tonyd.call("skill_delete", name=name)


@_tool()
def calibration(trim: dict[str, int] | None = None, limits: dict[str, list[int] | None] | None = None) -> dict:
    """Read or change calibration: trim = counts added to every target of a servo (-200..200; 0 removes),
    limits = soft [lo, hi] each target is clamped to (null removes). Applies to every motion immediately."""
    with _robot():
        return tonyd.call("calibration", trim=trim, limits=limits)["calibration"]


@_tool()
def servo_calibration() -> dict:
    """What each servo stores in itself (EEPROM): offset (-125..125) and angle limits [lo, hi]."""
    with _robot():
        return tonyd.call("servo_calibration")["servos"]


@_tool()
def servo_calibrate(servo_id: int, reason: str, offset: int | None = None, limits: list[int] | None = None,
                    save: bool = False) -> dict:
    """Change a servo's own offset and/or angle limits. Only to fix a bad calibration (e.g. a joint that sits off
    centre at 500, or limits that block a needed range): reason must say what is wrong and how you know. save=True
    writes the offset to the servo's EEPROM (limits always persist). Prefer calibration() trim for small fixes."""
    with _robot():
        return tonyd.call("servo_calibrate", id=servo_id, offset=offset, limits=limits, save=save, reason=reason)


@_tool()
def tether(on: bool | None = None, max_steps: int | None = None, reset: bool = False) -> dict:
    """Charging-cable tether. While on, walking skills (walk_*, step_left/right, any skill named *walk*) may use at
    most max_steps steps in total (default 10) so the cable isn't dragged; turns don't count. reset=True after
    walking back to the charger; on=False once unplugged. No arguments = just read it."""
    with _robot():
        return tonyd.call("tether", on=on, max_steps=max_steps, reset=reset)["tether"]


# ---- body learning (tonypi.learn; docs/BODY_LEARNING.md) -----------------------------------------------------------

MCP_BUDGET_S = 200  # a learning call returns within this (the brain's MCP tool timeout is 240 s); rerun for more


def _lab():
    from tonypi.learn.runner import Lab

    return Lab(tonyd=tonyd)


@contextmanager
def _learning():
    from tonypi.learn.runner import LabError

    try:
        yield
    except (LabError, TonydError, OSError, ValueError) as e:
        raise ToolError(str(e)) from e


def _spec(skill, frames, base, gait, count, guard_tilt_deg, objective, label, trace=False, start=None,
          setup=None) -> dict:
    return {"skill": skill, "frames": frames, "base": base, "gait": gait, "count": count, "objective": objective,
            "guard_tilt_deg": guard_tilt_deg, "label": label, "trace": trace, "start": start, "setup": setup}


@_tool()
def trial(skill: str | None = None, frames: list[dict] | None = None, base: str | None = None,
          gait: dict | None = None, count: int = 1, trials: int = 3, guard_tilt_deg: float | None = None,
          objective: str = "generic", label: str | None = None, trace: bool = False, start: str = "stand",
          setup: str | None = None) -> dict:
    """Measured trials of ONE move, the way to judge a move by numbers. Give one of: skill (a name), frames (your
    keyframes, repeated `count` times), or base (a motion name) + gait {time_scale, amplitude, offset {servo: delta},
    mirror, repeat}. objective: walk | turn | balance | pose | recover | generic (sets the score and the default
    guard: 40 deg for walk/turn, none for recover, 30 otherwise). Each trial: preflight (refuses if busy, stop latch,
    battery < 10.9 V, a servo >= 70 C; gets up first if lying), settle, run with the guard, phone recording in
    companion mode, score, ledger line; a fall is data (get_up, max twice, then it stops). start="as_is" skips the
    get_up and settle; setup=<skill> runs that skill unmeasured before each trial (implies as_is): measure getting up
    with skill="get_up", setup="lie_down", objective="recover" (key defaults to "get_up@lie_down"). trials 1-30,
    stops early at ~200 s. Returns per-trial score/tilt/yaw/displacement and a summary (mean, std, falls);
    `stopped` says why it ended early."""
    with _learning():
        return _lab().batch(_spec(skill, frames, base, gait, count, guard_tilt_deg, objective, label, trace, start,
                                  setup), trials, budget_s=MCP_BUDGET_S)


@_tool()
def compare(base: dict | str, candidate: dict | str, trials: int = 5, objective: str | None = None) -> dict:
    """A/B test, interleaved (base, candidate, base, ...). Each side is a skill name or a spec dict like trial's
    arguments ({"skill"} | {"frames", "count"} | {"base", "gait", "count"}, plus guard_tilt_deg, start, setup).
    trials per side (>= 5 for a verdict). verdict: "promote" (candidate mean beats base by > 1 pooled std, 0 falls;
    then save it with save_motion/save_skill), "worse", "no clear difference", "reject: the candidate fell more",
    or "more trials needed". Results go to the ledger and the library."""
    with _learning():
        return _lab().compare(base, candidate, trials, objective, budget_s=MCP_BUDGET_S)


@_tool()
def body_sweep(joints: list[int] | None = None, lying: bool = True) -> dict:
    """Map joints by measurement: lying=True lies down (legs unloaded) and sweeps each joint +-150 counts in 3 s ramps
    (default the 10 leg servos), lying=False stands and sweeps (default the 6 arm servos); then gets up / settles.
    Per joint: sign (+1 follows the command, 0 blocked), range reached, error, speed, IMU response. Saved to
    body_model(). Stops at ~200 s: rerun with the joints it lists as not swept."""
    with _learning():
        return _lab().sweep(joints, lying, budget_s=MCP_BUDGET_S)


@_tool()
def ledger(skill: str | None = None, label: str | None = None, limit: int = 20) -> list[dict]:
    """The newest learning records (trials and sweeps), oldest first: filter by skill key / candidate hash / what,
    and by label. Each has exp id, score, metrics, verdict, job id, phone episode id and a one-line note."""
    with _learning():
        return _lab().ledger(skill, label, max(1, min(200, int(limit))))


@_tool()
def body_model() -> dict:
    """What body_sweep measured, per servo: name, pose (lying/standing), base, plus/minus target and reached,
    sign, range_seen, speed_cps, max_error, rot_deg, n, t."""
    with _learning():
        return _lab().body_model()


@_tool()
def library() -> dict:
    """The skill library: per skill key, the objective, candidates (what, params, n, mean, std, falls), incumbent
    and best candidate hashes, promote_suggested, surface, evidence (exp ids)."""
    with _learning():
        return _lab().library()


@_tool()
def simulation_status() -> dict:
    """Pinned PAB47/TonyPi installation, MuJoCo readiness and simulation-to-hardware limitations. Read only."""
    from tonypi.simulation.service import status

    return status()


@_tool()
def simulation_catalog() -> dict:
    """Discover all upstream .d6a actions and Tony's current motions/skills, with simulation support reasons.
    Identifiers are upstream:NAME, motion:NAME, skill:NAME. Never moves the robot."""
    from tonypi.simulation.catalog import catalog, repository

    return catalog(repository(), tonyd)


@_tool()
def simulate_motion(motion: str = "", frames: list[dict] | None = None, count: int = 1, start: str = "home",
                    initial_raw: dict | None = None, objective: str = "stability", settle_s: float = 1.0,
                    render: bool = True) -> dict:
    """Run headless MuJoCo, NEVER hardware. Select a catalog motion OR motion-space frames (servos1..16).
    start=home|prone|supine; optional initial_raw gives all 16 measured raw pulses for an upright home start.
    objective=stability|recover. Returns measured model-only success, tilt, displacement, clipping, report id
    and an optional PNG (start/middle/end). Success cannot authorize body work or prove real-world safety.
    Scripted skills/live IK generation are unsupported. Count1..5, <=120 motion seconds, <=90 wall seconds."""
    from tonypi.simulation.service import simulate

    try:
        return simulate(tonyd, motion, frames, count, start, initial_raw, objective, settle_s, render)
    except (OSError, ValueError, RuntimeError) as e:
        raise ToolError(str(e)) from e


@_tool()
def simulation_execute(report_id: str) -> dict:
    """PHYSICAL BODY WORK: only with current owner authorization, never after STOP or for simulation-only tasks.
    Separately run one 30-degree-guarded Lab trial for an unclipped successful simulation report; locks/preflight,
    stop/cancellation, recovery, stable finish and ledger use existing interfaces. Never clears a stop latch.
    Requires unchanged frames/calibration, upright initial pose within20 raw counts, battery>=10.9V, temp<70C.
    All bridge trials are refused while tethered; native skills retain their name and count.
    Model success is only a screening step; inspect physical results and repeat trials before promotion."""
    from tonypi.simulation.service import execute

    with _learning():
        return execute(tonyd, report_id)


@_tool()
def promote_skill(name: str, report_id: str, description: str = "") -> dict:
    """LEARNING ONLY: promote immutable count-one frames after a matching model pass and five matched no-fall jobs.
    Reads evidence; never moves. Missing/stale simulation, calibration, trial or job provenance is refused."""
    from tonypi.motion.promotion import promote
    from tonypi.simulation.catalog import calibration as current_calibration, root
    from tonypi.tonyd.studio import USER_DIR

    if len(report_id) != 32 or any(c not in "0123456789abcdef" for c in report_id):
        raise ToolError("report_id must be 32 lowercase hexadecimal characters")
    with _learning():
        result = promote(name, root() / "reports" / report_id, USER_DIR / "brain/learning/ledger.jsonl",
                         USER_DIR / "jobs.jsonl", USER_DIR, current_calibration(tonyd), description)
    return {k: v for k, v in result.items() if k != "frames"}


@_tool()
def say(text: str) -> dict:
    """Speak out loud through Tony's speaker (interrupts nothing; returns as soon as speaking starts)."""
    if not text.strip() or len(text) > 2000:
        raise ToolError("speech must contain 1..2000 characters")
    errors = []
    for path in (VOICE_SOCK, AGENT_SOCK):  # live mode (tonypi-voice) first, then the local fallback agent
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(5)
        try:
            s.connect(path)
            s.sendall((json.dumps({"op": "say", "text": text}) + "\n").encode())
            s.makefile("r").readline()
            return {"said": text}
        except OSError as e:
            errors.append(f"{path}: {e}")
        finally:
            s.close()
    raise ToolError("no speech service reachable (" + "; ".join(errors) + ")")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser()
    parser.add_argument("--transport", choices=("streamable-http", "stdio"), default="streamable-http")
    args = parser.parse_args()
    if PROFILE == "learning" and args.transport != "stdio":
        parser.error("learning profile requires explicit local stdio transport")
    if args.transport == "stdio":
        mcp.run("stdio")
        return
    host = os.environ.get("TONYPI_MCP_HOST", "127.0.0.1")
    port = int(os.environ.get("TONYPI_MCP_PORT", "8765"))
    mcp.run("streamable-http", host=host, port=port)


if __name__ == "__main__":
    main()
