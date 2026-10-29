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
        "speaker). Use run_skill for every body movement; one skill runs at a time and a new one preempts the "
        "last. stop() halts immediately. Walking is open-loop and drifts: look (snapshot) after moving. "
        "Servos switch off automatically when idle and joints refuse to move when too hot; that is expected."
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
def list_skills() -> dict[str, str]:
    """Every skill the body can run, with what it does. count repeats steps for walking/turning skills."""
    with _robot():
        return tonyd.skills()


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
