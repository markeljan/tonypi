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
        "it in small steps; check max_tilt_deg in the result. tonyd still stops anything that overheats or tips "
        "past 70 deg, and switches servos off (after a neutral stand) when idle unless you hold a pose."
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
def run_skill(skill: str, count: int = 1, wait: bool = True) -> dict:
    """Run one body skill (see list_skills). count = steps/repeats (1-5). With wait=True, returns when it ends:
    state is done, failed (refused: too hot, fell, ...; see `said`) or cancelled (preempted or stopped)."""
    with _robot():
        return tonyd.run(skill, count, wait=wait)


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


@mcp.tool()
def get_joints() -> dict:
    """Current bus servo positions: `pos` in motion space (what motions use) and `raw` servo counts."""
    with _robot():
        return tonyd.joints()


@mcp.tool()
def play_frames(frames: list[dict], hold_s: float = 0) -> dict:
    """Play your own keyframes without saving them: [{"ms": 400, "pos": {"4": 620, "12": 380}}, ...]. Servos left
    out of a frame keep their position. Same guards as skills. hold_s > 0 keeps torque on (holding the last pose)
    that long afterwards instead of settling to a stand. Returns the job, with max_tilt_deg."""
    with _robot():
        return tonyd.play(frames, hold_s=hold_s)


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
def save_skill(name: str, motions: list[str], description: str = "", upright: bool = True,
               settle: bool = True) -> dict:
    """Define a skill as a motion sequence; "*name" repeats with count. E.g. a better walk:
    save_skill("walk_forward", ["my_walk_start", "*my_step", "my_walk_end"]). upright: get up first if lying.
    settle: stand before going limp afterwards."""
    with _robot():
        return tonyd.call("skill_save", name=name, motions=motions, description=description, upright=upright,
                          settle=settle)


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
