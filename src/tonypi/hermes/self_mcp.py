"""tonypi-self: MCP tools (stdio, launched by Hermes) for Tony's task reporting and self-update.

- task_done: Tony says a task is finished (or that he has truly run out of options). The front end keeps a task
  going with "not done yet, try another way" turns until this is called, "stop" is said, or time runs out.
- self_*: read and change his own SOUL, skills and harness code (an allow-list in this repo), run the tests,
  commit on the branch, and reload. SOUL and skills apply on the next turn; code and config after the current task
  (the front end re-execs itself / restarts the Hermes gateway when idle).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

REPO = Path(os.environ.get("TONYPI_REPO", Path(__file__).resolve().parents[3]))
PROFILE = Path(os.environ.get("TONYPI_HERMES_PROFILE", Path.home() / ".hermes/profiles/tonypi"))
STATE = Path(os.environ.get("TONYPI_HERMES_STATE", Path.home() / ".local/state/tonypi/hermes"))
REPORT = STATE / "task_report.json"
RELOAD = STATE / "reload.json"  # read by the front end
CHANGES = STATE / "self_changes.jsonl"
PYTHON = os.environ.get("TONYPI_PYTHON", "/home/pi/tonypi/.venv/bin/python")
# what Tony may change in himself (relative to the repo); everything else is read-only to him
EDITABLE = ("hermes/SOUL.md", "hermes/memory-seed.md", "hermes/config.yaml", "hermes/skills/",
            "src/tonypi/hermes/", "tests/test_hermes_", "tools/hermes_")
BUNDLED_SKILL_DIRS = {"autonomous-ai-agents"}  # Hermes' own; not part of the repo

mcp = MCPServer(
    "tonypi-self",
    instructions=(
        "Tony's own task reporting and self-update. Call task_done when a task is finished or when you have truly "
        "run out of options. To learn or improve: change your skills (skill_manage or self_write), SOUL or harness "
        "code, run self_test after code changes, then self_commit, then self_reload."
    ),
)


def _rel(path: str) -> Path:
    p = (REPO / path).resolve()
    rel = p.relative_to(REPO.resolve()).as_posix() if p.is_relative_to(REPO.resolve()) else None
    if rel is None or not any(rel == e or rel.startswith(e) for e in EDITABLE):
        raise ToolError(f"{path}: not one of Tony's editable files ({', '.join(EDITABLE)})")
    return p


def _log(kind: str, **kw) -> None:
    STATE.mkdir(parents=True, exist_ok=True)
    with CHANGES.open("a") as f:
        f.write(json.dumps({"t": round(time.time(), 1), "kind": kind, **kw}) + "\n")


def _install_file(path: str) -> list[str]:
    """One repo file → the Hermes profile (SOUL or a skill file): applies on the next turn."""
    if path == "hermes/SOUL.md":
        shutil.copy(REPO / path, PROFILE / "SOUL.md")
        return ["SOUL.md"]
    if path.startswith("hermes/skills/"):
        dst = PROFILE / "skills" / Path(path).relative_to("hermes/skills")
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(REPO / path, dst)
        return [dst.relative_to(PROFILE).as_posix()]
    return []


def _install_persona_and_skills() -> list[str]:
    """Repo SOUL + robot skills → Hermes profile, after pulling Tony's own skill edits into the repo (none lost)."""
    _sync_skills_back()
    shutil.copy(REPO / "hermes/SOUL.md", PROFILE / "SOUL.md")
    done = ["SOUL.md"]
    for d in (REPO / "hermes/skills").iterdir():
        if d.is_dir():
            shutil.copytree(d, PROFILE / "skills" / d.name, dirs_exist_ok=True)
            done.append(f"skills/{d.name}")
    return done


def _sync_skills_back() -> list[str]:
    """Skills Hermes wrote or edited in its profile (skill_manage) → repo, so they get committed."""
    changed = []
    for d in (PROFILE / "skills").iterdir():
        if not d.is_dir() or d.name in BUNDLED_SKILL_DIRS or d.name.startswith("."):
            continue
        for f in d.rglob("*"):
            if f.is_file() and not f.name.startswith("."):
                dst = REPO / "hermes/skills" / f.relative_to(PROFILE / "skills")
                if not dst.exists() or dst.read_bytes() != f.read_bytes():
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy(f, dst)
                    changed.append(dst.relative_to(REPO).as_posix())
    return changed


def commit_learned(context: str) -> str | None:
    """Skills Tony changed in his profile (skill_manage) → repo → a commit of just those files. Returns the commit."""
    changed = _sync_skills_back()
    if not changed:
        return None
    subprocess.run(["git", "add", "--", *changed], cwd=REPO, check=True, capture_output=True)
    names = sorted({Path(c).parent.name for c in changed})
    r = subprocess.run(["git", "commit", "-q", "-m", f"Tony learned: {', '.join(names)}\n\nAfter: {context[:200]}\n\n"
                        "Self-update by Tony (Hermes harness).", "--", *changed], cwd=REPO, capture_output=True, text=True)
    if r.returncode:
        return None
    head = subprocess.run(["git", "log", "--oneline", "-1"], cwd=REPO, capture_output=True, text=True).stdout.strip()
    _log("learned", files=changed, head=head)
    return head


@mcp.tool()
def task_done(success: bool, summary: str, how: str = "") -> dict:
    """Report the current task as finished. success=True: it's done (you checked: camera / get_state / yaw).
    success=False: only when you have truly run out of options; say what you tried and what you need.
    summary: one spoken-style sentence. how: a short note on how you did it (for the log)."""
    STATE.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps({"t": time.time(), "success": success, "summary": summary, "how": how}))
    return {"recorded": True}


@mcp.tool()
def self_files() -> list[str]:
    """Your editable files: SOUL, skills, Hermes config, harness code and its tests."""
    out = []
    for e in EDITABLE:
        p = REPO / e
        if p.is_file():
            out.append(e)
        elif p.is_dir():
            out += sorted(f.relative_to(REPO).as_posix() for f in p.rglob("*") if f.is_file()
                          and "__pycache__" not in f.parts)
        else:
            out += sorted(f.relative_to(REPO).as_posix() for f in p.parent.glob(p.name + "*") if f.is_file())
    return out


@mcp.tool()
def self_read(path: str) -> str:
    """Read one of your editable files (path relative to the repo, e.g. hermes/SOUL.md)."""
    p = _rel(path)
    if not p.is_file():
        raise ToolError(f"{path}: no such file")
    return p.read_text()


@mcp.tool()
def self_write(path: str, content: str, why: str) -> dict:
    """Create or replace one of your editable files. why: what you are improving. SOUL and skills apply on your next
    turn; code needs self_test, then self_reload("front")."""
    p = _rel(path)
    old = p.read_text() if p.is_file() else ""
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    _log("write", path=path, why=why, old_len=len(old), new_len=len(content))
    applied = _install_file(path)
    return {"written": path, "bytes": len(content), "applied_now": applied}


@mcp.tool()
def self_test() -> dict:
    """Run the harness tests (tests/test_hermes_*.py). Do this after any code change, before self_reload."""
    r = subprocess.run([PYTHON, "-m", "pytest", "-q", *sorted(map(str, REPO.glob("tests/test_hermes_*.py")))],
                       cwd=REPO, capture_output=True, text=True, timeout=300,
                       env={**os.environ, "PYTHONPATH": str(REPO / "src")})
    tail = (r.stdout + r.stderr).strip().splitlines()[-15:]
    return {"passed": r.returncode == 0, "output": "\n".join(tail)}


@mcp.tool()
def self_commit(message: str) -> dict:
    """Commit your changes (skills you edited with skill_manage are copied in first) on the harness branch."""
    synced = _sync_skills_back()
    paths = [e.rstrip("/") for e in EDITABLE if not e.endswith("_")] + \
        [f.relative_to(REPO).as_posix() for e in EDITABLE if e.endswith("_") for f in REPO.glob(e + "*")]
    subprocess.run(["git", "add", "--", *paths], cwd=REPO, check=True, capture_output=True)
    if subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=REPO).returncode == 0:
        return {"committed": False, "reason": "nothing changed", "synced_skills": synced}
    r = subprocess.run(["git", "commit", "-q", "-m", f"{message}\n\nSelf-update by Tony (Hermes harness)."],
                       cwd=REPO, capture_output=True, text=True)
    if r.returncode:
        raise ToolError(f"commit failed: {r.stderr.strip()[:300]}")
    head = subprocess.run(["git", "log", "--oneline", "-1"], cwd=REPO, capture_output=True, text=True).stdout.strip()
    _log("commit", message=message, head=head, synced_skills=synced)
    return {"committed": True, "commit": head, "synced_skills": synced}


@mcp.tool()
def self_reload(what: str) -> dict:
    """Apply changes. what = "persona" (SOUL + skills from the repo into Hermes, now), "front" (harness code: the
    front end restarts itself after this task; run self_test first) or "gateway" (Hermes config: Hermes restarts
    after this task)."""
    if what == "persona":
        return {"applied": _install_persona_and_skills()}
    if what not in ("front", "gateway"):
        raise ToolError('what must be "persona", "front" or "gateway"')
    if what == "front":
        r = subprocess.run([PYTHON, "-c", "import tonypi.hermes.front, tonypi.hermes.peripherals, tonypi.hermes.keeper"],
                           capture_output=True, text=True, env={**os.environ, "PYTHONPATH": str(REPO / "src")})
        if r.returncode:
            raise ToolError(f"not reloading: the front end doesn't import: {r.stderr.strip()[-400:]}")
    STATE.mkdir(parents=True, exist_ok=True)
    want = json.loads(RELOAD.read_text()) if RELOAD.exists() else {}
    want[what] = time.time()
    RELOAD.write_text(json.dumps(want))
    _log("reload", what=what)
    return {"scheduled": what, "when": "after the current task"}


def main() -> None:
    mcp.run("stdio")


if __name__ == "__main__":
    main()
