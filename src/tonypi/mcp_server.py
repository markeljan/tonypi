"""tonypi-mcp: the robot as an MCP server (Streamable HTTP), for Claude Code, Hermes, or any MCP client.

A thin layer over tonyd's socket (body, camera) and the agent's socket (speech). It holds no robot state and
never touches the hardware, so it can restart freely. Default: http://127.0.0.1:8765/mcp
"""

from __future__ import annotations

import json
import logging
import os
import socket
from contextlib import contextmanager

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.mcpserver.utilities.types import Image

from tonypi.tonyd.client import TonydClient, TonydError

AGENT_SOCK = os.environ.get("TONYPI_SOCK", "/run/tonypi/agent.sock")

mcp = MCPServer(
    "tonypi",
    instructions=(
        "You control Tony, a 35 cm Hiwonder TonyPi humanoid robot (16 leg/arm servos, 2-axis head with a camera, "
        "speaker). run_skill runs a named skill; one body job runs at a time and a new one preempts the last. "
        "stop() halts immediately. Walking is open-loop and drifts: look (snapshot) after moving. "
        "You can also write your own movement: read joints, play keyframes, save motions and skills (override "
        "walk/turn/step with better gaits), and calibrate. 16 bus servos, positions 0..1000 (500 = centre, "
        "~0.24 deg/count): L/R 1/9 ankle roll, 2/10 ankle pitch, 3/11 knee, 4/12 hip pitch, 5/13 hip roll, "
        "6/14 elbow, 7/15 shoulder roll, 8/16 shoulder pitch. Start from a default motion (get_motion) and change "
        "it in small steps; check max_tilt_deg in the result. Falls are yours to handle: nothing stops a move for "
        "tilting unless you pass guard_tilt_deg, and nothing stands Tony up unless you run settle / get_up. tonyd "
        "only stops what overheats, and switches servos off 1.5 s after the last move (in whatever pose; end in a "
        "stable one, or hold_s) to keep them cool. While plugged into the charger (tether on) walking is limited to "
        "a step budget; see tether()."
    ),
)
tonyd = TonydClient()


@contextmanager
def _robot():
    """Robot-side failures (unknown skill, no camera, tonyd down) go back to the model as readable errors."""
    try:
        yield
    except (TonydError, OSError) as e:
        raise ToolError(str(e)) from e


@mcp.tool()
def list_skills() -> dict:
    """Every skill the body can run: description, source (builtin / default / user) and, for sequence skills,
    the motions it plays. count repeats steps for walking/turning skills."""
    with _robot():
        return tonyd.call("skills")["details"]


@mcp.tool()
def run_skill(skill: str, count: int = 1, wait: bool = True, guard_tilt_deg: float | None = None) -> dict:
    """Run one body skill (see list_skills). count = steps/repeats (1-5). With wait=True, returns when it ends:
    state is done, failed (refused: too hot, ...; see `said`) or cancelled (preempted or stopped). max_tilt_deg says
    how far Tony leaned. guard_tilt_deg (opt-in) stops the skill if he tilts past that angle."""
    with _robot():
        return tonyd.call("run", timeout=185 if wait else 10, skill=skill, count=count, wait=wait, wait_s=180,
                          guard_tilt_deg=guard_tilt_deg)["job"]


@mcp.tool()
def wait_motion(timeout_s: float = 3.0) -> dict:
    """Wait up to timeout_s (max 3) for the running body job to finish, then report it. Use after
    run_skill(wait=False) in a loop for long moves: you stay steerable between the short waits."""
    import time

    end = time.monotonic() + max(0.0, min(3.0, float(timeout_s)))
    with _robot():
        while True:
            st = tonyd.state()
            if not st["busy"] or time.monotonic() >= end:
                return {"busy": st["busy"], "job": st["job"], "tilt_deg": st["tilt_deg"], "lying": st["lying"],
                        "heading_deg": st.get("heading_deg"), "stop_latched": st.get("stop_latched")}
            time.sleep(0.2)


@mcp.tool()
def stop() -> dict:
    """Stop whatever the body is doing, immediately."""
    with _robot():
        tonyd.stop()
    return {"stopped": True}


@mcp.tool()
def get_state() -> dict:
    """Posture (tilt, lying, asleep), battery mV, servo temperatures (degrees C), current skill, head angle,
    camera status."""
    with _robot():
        return tonyd.state(fresh=True)


@mcp.tool()
def look(pan_deg: float = 0.0, tilt_deg: float = 0.0) -> dict:
    """Point the head (and camera). pan_deg: + left / - right, -60..60. tilt_deg: + up / - down, -40..25.
    0, 0 is straight ahead."""
    with _robot():
        return tonyd.head(pan_deg, tilt_deg)


@mcp.tool()
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


@mcp.tool()
def scan(pans: list[float] | None = None, tilt_deg: float | None = None) -> list:
    """Look around without moving the body: one photo per head pan angle (default 60, 0, -60; + = left, max 7
    angles, -60..60), each labelled with its angle, then the head returns. Use it to find things outside the
    current view before turning or walking."""
    with _robot():
        r = tonyd.call("scan", timeout=30, pans=pans or [60, 0, -60], tilt_deg=tilt_deg)
    return [f"heading {r['heading_deg']} deg"] + _images(
        r["views"], lambda v: f"head pan {v['pan_deg']:+.0f} deg, tilt {v['tilt_deg']:+.0f} deg")


@mcp.tool()
def video(seconds: float = 2.0, frames: int = 4) -> list:
    """A short clip: `frames` photos (2-12) spread over `seconds` (0.2-10), oldest first, each with its time.
    To watch a move, start it with run_skill(..., wait=False) or play_frames first, then call this."""
    with _robot():
        r = tonyd.call("burst", timeout=float(seconds) + 15, frames=frames, seconds=seconds)
    return _images(r["frames"], lambda f: f"t = {f['t_s']:.1f} s")


@mcp.tool()
def get_joints() -> dict:
    """Current bus servo positions: `pos` in motion space (what motions use) and `raw` servo counts."""
    with _robot():
        return tonyd.joints()


@mcp.tool()
def play_frames(frames: list[dict], hold_s: float = 0, guard_tilt_deg: float | None = None, wait: bool = True) -> dict:
    """Play your own keyframes without saving them: [{"ms": 400, "pos": {"4": 620, "12": 380}}, ...]. Servos left
    out of a frame keep their position. hold_s > 0 keeps torque on (holding the last pose) that long afterwards,
    otherwise Tony goes limp in the last pose 1.5 s after. Returns the job, with max_tilt_deg."""
    with _robot():
        return tonyd.call("play", timeout=185 if wait else 10, frames=frames, hold_s=hold_s,
                          guard_tilt_deg=guard_tilt_deg, wait=wait, wait_s=180)["job"]


@mcp.tool()
def set_pose(positions: dict[str, int], ms: int = 500, hold_s: float = 10) -> dict:
    """Move some servos to positions over `ms` and hold for hold_s (max 120). For calibrating and trying poses."""
    with _robot():
        return tonyd.pose(positions, ms=ms, hold_s=hold_s)


@mcp.tool()
def set_torque(on: bool, ids: list[int] | None = None, hold_s: float = 0) -> dict:
    """Torque off (limp: pose Tony by hand, then get_joints to record it) or on for the given servos (default
    all 16). On is held for hold_s, then the idle rule switches servos off again."""
    with _robot():
        tonyd.torque(ids, on, hold_s=hold_s)
    return {"torque": on}


@mcp.tool()
def list_motions() -> list[dict]:
    """Every motion (keyframe file): defaults from the factory library, and saved user motions (which override a
    default of the same name)."""
    with _robot():
        return tonyd.call("motions")["motions"]


@mcp.tool()
def get_motion(name: str) -> dict:
    """A motion's keyframes, to study or copy before writing your own version."""
    with _robot():
        return tonyd.call("motion_get", name=name)


@mcp.tool()
def save_motion(name: str, frames: list[dict], description: str = "") -> dict:
    """Save keyframes as a named motion (a default's name overrides it; delete_motion restores the default)."""
    with _robot():
        return tonyd.call("motion_save", name=name, frames=frames, description=description)


@mcp.tool()
def delete_motion(name: str) -> dict:
    """Delete a user motion (restores the default of that name, if any)."""
    with _robot():
        return tonyd.call("motion_delete", name=name)


@mcp.tool()
def save_skill(name: str, motions: list[str], description: str = "") -> dict:
    """Define a skill as a motion sequence; "*name" repeats with count. E.g. a better walk:
    save_skill("walk_forward", ["my_walk_start", "*my_step", "my_walk_end"])."""
    with _robot():
        return tonyd.call("skill_save", name=name, motions=motions, description=description)


@mcp.tool()
def delete_skill(name: str) -> dict:
    """Delete a user skill (restores the default skill of that name, if any)."""
    with _robot():
        return tonyd.call("skill_delete", name=name)


@mcp.tool()
def calibration(trim: dict[str, int] | None = None, limits: dict[str, list[int] | None] | None = None) -> dict:
    """Read or change calibration: trim = counts added to every target of a servo (-200..200; 0 removes),
    limits = soft [lo, hi] each target is clamped to (null removes). Applies to every motion immediately."""
    with _robot():
        return tonyd.call("calibration", trim=trim, limits=limits)["calibration"]


@mcp.tool()
def servo_calibration() -> dict:
    """What each servo stores in itself (EEPROM): offset (-125..125) and angle limits [lo, hi]."""
    with _robot():
        return tonyd.call("servo_calibration")["servos"]


@mcp.tool()
def servo_calibrate(servo_id: int, reason: str, offset: int | None = None, limits: list[int] | None = None,
                    save: bool = False) -> dict:
    """Change a servo's own offset and/or angle limits. Only to fix a bad calibration (e.g. a joint that sits off
    centre at 500, or limits that block a needed range): reason must say what is wrong and how you know. save=True
    writes the offset to the servo's EEPROM (limits always persist). Prefer calibration() trim for small fixes."""
    with _robot():
        return tonyd.call("servo_calibrate", id=servo_id, offset=offset, limits=limits, save=save, reason=reason)


@mcp.tool()
def tether(on: bool | None = None, max_steps: int | None = None, reset: bool = False) -> dict:
    """Charging-cable tether. While on, walking skills (walk_*, step_left/right, any skill named *walk*) may use at
    most max_steps steps in total (default 10) so the cable isn't dragged; turns don't count. reset=True after
    walking back to the charger; on=False once unplugged. No arguments = just read it."""
    with _robot():
        return tonyd.call("tether", on=on, max_steps=max_steps, reset=reset)["tether"]


@mcp.tool()
def say(text: str) -> dict:
    """Speak out loud through Tony's speaker (interrupts nothing; returns as soon as speaking starts)."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(5)
    try:
        s.connect(AGENT_SOCK)
        s.sendall((json.dumps({"op": "say", "text": text}) + "\n").encode())
        s.makefile("r").readline()
    except OSError as e:
        raise ToolError(f"tonypi-agent (speech) not reachable at {AGENT_SOCK}: {e}") from e
    finally:
        s.close()
    return {"said": text}


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    host = os.environ.get("TONYPI_MCP_HOST", "127.0.0.1")
    port = int(os.environ.get("TONYPI_MCP_PORT", "8765"))
    mcp.run("streamable-http", host=host, port=port)


if __name__ == "__main__":
    main()
