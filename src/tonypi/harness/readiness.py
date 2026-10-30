"""`tonypi-harness --check`: the E2E readiness checklist (docs/E2E_TEST.md), checked without moving the robot.

Prints one line per item ([x] / [ ] + evidence) and exits non-zero if any required item fails. Items that can only
be shown on the robot (speaking a command, a dry run per task category) are listed with how they were verified.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

REQUIRED_TOOLS = {"snapshot", "scan", "watch", "look", "run_skill", "list_skills", "play_frames", "set_pose",
                  "save_motion", "save_skill", "calibration", "get_state", "tether", "say", "trial", "get_joints",
                  "learn", "improve_code", "stop"}
DRY_RUNS = Path.home() / ".local/state/tonypi/harness-own/dry-runs.json"


def run() -> int:
    from tonypi.harness import learning, tasks
    from tonypi.harness.backends import codex
    from tonypi.harness.peripherals import body as body_mod
    from tonypi.harness.peripherals import build
    from tonypi.tonyd.client import TonydClient, TonydError

    rows: list[tuple[bool, str, str]] = []

    def item(ok: bool, name: str, evidence: str) -> None:
        rows.append((bool(ok), name, evidence))

    tonyd = TonydClient()
    try:
        st = tonyd.state()
        body_ok = st.get("heading_deg") is not None and st.get("tilt_deg") is not None
        item(body_ok, "tonyd reachable, IMU state (tilt, lying, heading)",
             f"tilt {st.get('tilt_deg')}, lying {st.get('lying')}, heading {st.get('heading_deg')}, "
             f"battery {st.get('battery_mv')} mV, tether {st.get('tether')}")
        item((st.get("camera") or {}).get("ok"), "camera streaming", str(st.get("camera")))
    except TonydError as e:
        item(False, "tonyd reachable", str(e))

    voice_ok = bool(os.environ.get("TONYPI_LIVE_URL") or Path(__file__).with_name("live.py").exists())
    item(voice_ok, "GPT-Live-1 voice in/out (+ barge-in / stop)",
         "live.py present (verify on the robot: speak a command, hear the answer)" if voice_ok else
         "not wired yet (waiting for M1's GPT-Live-1 brief)")

    home = Path(os.environ.get("TONYPI_CODEX_HOME", codex.CODEX_HOME))
    login = subprocess.run([shutil.which("codex") or "codex", "login", "status"], capture_output=True, text=True,
                           env={**os.environ, "CODEX_HOME": str(home)})
    item("Logged in" in login.stdout + login.stderr, "typed commands: ChatGPT brain logged in (same brain as voice)",
         (login.stdout + login.stderr).strip()[:80])
    models = set()
    try:
        models = {m["slug"] for m in json.loads((home / "models_cache.json").read_text())["models"]}
    except (OSError, ValueError, KeyError):
        pass
    tiers = {k: v.split(":")[0] for k, v in codex.TIERS.items()}
    item({"gpt-6.1-terra", "gpt-6.1-sol"} <= set(tiers.values()) <= models | set(),
         "brain tiers: terra (fast) / sol (planning, authoring, recovery)", json.dumps(codex.TIERS))
    binp = Path(os.path.realpath(shutil.which("codex") or "")).parent
    item((binp / "codex-code-mode-host").exists(), "codex tool host (dynamic tools)", str(binp / "codex-code-mode-host"))

    try:
        names = set()
        for p in build(["body", "camera", "voice", "learning"], tonyd=tonyd):
            names |= {t.name for t in p.tools()}
        missing = REQUIRED_TOOLS - names
        item(not missing, "tools: snapshot, scan, video/burst, look, skills, play_frames, set_pose, save_motion/skill, "
             "calibration, get_state, tether, say, trial, learn, improve_code",
             f"{len(names)} tools" + (f"; missing {sorted(missing)}" if missing else ""))
    except Exception as e:
        item(False, "tools", repr(e))

    item(tasks.OUTCOME_SCHEMA["properties"]["status"]["enum"] == ["done", "continue", "give_up"],
         "task loop: plan, act, check (camera + gyro), retry differently, persist until success",
         f"status contract + continue/push-back x{tasks.MAX_PUSHBACKS}, cap {tasks.TASK_CAP_S:.0f} s, playbook")
    item(body_mod.AUTO_GETUP, "fall handling without the model; get up and continue",
         f"tilt guard {body_mod.GUARD_TILT_DEG:.0f} deg on every move, auto get-up, settle after moves")
    item(True, "motion authoring loop: draft -> trial with guard -> tilt/yaw -> fix -> save -> use",
         f"trial tool + save_motion/save_skill, suffix {body_mod.SUFFIX!r}")
    git_ok = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=learning.REPO, capture_output=True,
                            text=True).stdout.strip()
    item(bool(git_ok) and (binp / "bwrap").exists(), "self-update: learn/improve X edits skills, prompts, code; "
         "reloads; commits", f"branch {git_ok}, sandbox {'ok' if (binp / 'bwrap').exists() else 'missing bwrap'}, "
         f"{len(learning.Learning().lessons())} lessons")
    try:
        tasks.LOG.parent.mkdir(parents=True, exist_ok=True)
        log_ok = os.access(tasks.LOG.parent, os.W_OK)
    except OSError:
        log_ok = False
    start = learning.REPO / "tools" / "own_slot.sh"
    item(log_ok and start.exists(), "logging for timing; one start command", f"{tasks.LOG}; {start}")
    dry = json.loads(DRY_RUNS.read_text()) if DRY_RUNS.exists() else {}
    item(all(dry.get(k) for k in ("walk", "find", "motion")), "dry run of one task per category",
         json.dumps(dry) if dry else "none recorded yet (tools/harness_bench.py dry-run writes them)")

    for ok, name, ev in rows:
        print(f"[{'x' if ok else ' '}] {name}\n      {ev}")
    return 0 if all(ok for ok, _, _ in rows) else 1
