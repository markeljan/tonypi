"""Readiness checklist for the E2E test (docs/E2E_TEST.md), Hermes harness. Checks what can be checked from code;
the rest (voice on the robot, dry runs) is listed with how it was verified. No robot motion.

    uv run python tools/hermes_ready.py [--probe]     # --probe: one tiny Hermes turn (get_state + tether)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import socket
import subprocess
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SOCK = os.environ.get("TONYPI_SOCK", "/run/tonypi/agent.sock")
NEEDED = {"snapshot", "scan", "video", "look", "run_skill", "list_skills", "play_frames", "set_pose", "save_motion",
          "save_skill", "get_motion", "calibration", "get_state", "tether", "say", "stop", "get_joints"}
SELF = {"task_done", "self_write", "self_test", "self_commit", "self_reload"}


async def mcp_tools(url: str | None = None, command: list[str] | None = None, env: dict | None = None) -> set[str]:
    from mcp import ClientSession
    if url:
        from mcp.client.streamable_http import streamable_http_client as client
        ctx = client(url)
    else:
        from mcp.client.stdio import StdioServerParameters, stdio_client
        ctx = stdio_client(StdioServerParameters(command=command[0], args=command[1:], env=env))
    async with ctx as t:
        async with ClientSession(t[0], t[1]) as s:
            await s.initialize()
            return {tool.name for tool in (await s.list_tools()).tools}


def check(name: str, fn) -> tuple[str, bool, str]:
    try:
        ok, note = fn()
    except Exception as e:  # a failed check is a result, not a crash
        ok, note = False, f"{type(e).__name__}: {e}"
    return name, ok, note


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--probe", action="store_true", help="run one Hermes turn that must call get_state and tether")
    args = ap.parse_args()
    from tonypi.hermes.client import HermesRuns
    from tonypi.tonyd.client import TonydClient

    h, t = HermesRuns(), TonydClient()

    def gateway():
        return h.health(), h.url

    def codex():
        out = subprocess.run(["/home/pi/.local/bin/tonypi", "auth", "status", "openai-codex"], capture_output=True,
                             text=True, timeout=60).stdout.strip()
        return "logged in" in out, out.splitlines()[-1] if out else "?"

    def robot_tools():
        have = asyncio.run(mcp_tools(url="http://127.0.0.1:8765/mcp"))
        return NEEDED <= have, f"{len(have)} tools; missing {sorted(NEEDED - have)}" if NEEDED - have else f"{len(have)} tools"

    def self_tools():
        have = asyncio.run(mcp_tools(command=["/home/pi/tonypi/.venv/bin/python", "-m", "tonypi.hermes.self_mcp"],
                                     env={**os.environ, "PYTHONPATH": str(REPO / "src"), "TONYPI_REPO": str(REPO)}))
        return SELF <= have, f"{sorted(have)}"

    def body():
        st = t.state(fresh=True)
        teth = t.call("tether")["tether"]
        ok = st["camera"]["ok"] and not st["lying"] and st["battery_mv"] > 10800
        return ok, (f"tilt {st['tilt_deg']}°, lying {st['lying']}, heading {st.get('heading_deg')}, battery "
                    f"{st['battery_mv']} mV, camera {st['camera']['ok']}, tether {teth}")

    def front():
        s = socket.socket(socket.AF_UNIX)
        s.settimeout(5)
        s.connect(SOCK)
        s.sendall(b'{"text": "/status"}\n')
        for line in s.makefile():  # broadcast events may come first
            if '"status"' in line:
                break
        s.close()
        ok = '"harness": "hermes"' in line
        return ok, f"{SOCK}: " + ("the Hermes front end" if ok else "answered, but not the Hermes front end (tonypi-agent?)")

    def keeper_and_log():
        from tonypi.hermes import front as f, keeper as k
        f.STATE.mkdir(parents=True, exist_ok=True)
        probe = f.STATE / ".write-test"
        probe.write_text("ok")
        probe.unlink()
        return True, f"task log {f.TASK_LOG} writable; keeper settles {k.SETTLE_AFTER_S} s after a move"

    def frozen():
        st = subprocess.run(["git", "status", "--porcelain"], cwd=REPO, capture_output=True, text=True).stdout
        head = subprocess.run(["git", "log", "--oneline", "-1"], cwd=REPO, capture_output=True, text=True).stdout
        return not st.strip(), f"{head.strip()}" + ("" if not st.strip() else f"; uncommitted: {st.split()[:6]}")

    def tests():
        r = subprocess.run(["/home/pi/tonypi/.venv/bin/python", "-m", "pytest", "-q",
                            *map(str, sorted(REPO.glob("tests/test_hermes_*.py")))], cwd=REPO, capture_output=True,
                           text=True, timeout=300, env={**os.environ, "PYTHONPATH": str(REPO / "src")})
        return r.returncode == 0, r.stdout.strip().splitlines()[-1]

    def probe():
        from tonypi.hermes.front import TIERS, PROVIDER
        t0 = time.monotonic()
        rid = h.start("Readiness probe: call get_state and tether, then reply with only the battery voltage.",
                      f"ready-{int(time.time())}", TIERS["fast"][0], PROVIDER, TIERS["fast"][1])
        tools, out = [], ""
        for ev in h.events(rid):
            if ev.get("event") == "tool.started":
                tools.append(str(ev.get("tool", "")).removeprefix("mcp__tonypi__"))
            if ev.get("event", "").startswith("run."):
                out = ev.get("output") or ev.get("error") or ev.get("event")
        ok = {"get_state", "tether"} <= set(tools)
        return ok, f"{time.monotonic() - t0:.1f} s, tools {tools}, said {out!r}"

    checks = [check("Hermes gateway (API :8642)", gateway), check("ChatGPT/Codex login", codex),
              check("Robot tools (tonypi-mcp :8765)", robot_tools), check("Self-update tools (tonypi-self)", self_tools),
              check("Body: upright, camera, battery, tether", body), check("Front end socket", front),
              check("Task log + keeper", keeper_and_log), check("Harness tests", tests),
              check("Code frozen (committed)", frozen)]
    if args.probe:
        checks.append(check("Brain probe (terra): get_state + tether", probe))
    for name, ok, note in checks:
        print(f"[{'x' if ok else ' '}] {name}: {note}")
    print(json.dumps({"ready": all(ok for _, ok, _ in checks)}))


if __name__ == "__main__":
    main()
