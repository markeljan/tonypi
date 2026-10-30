"""Self-update: the harness changes its own instructions, moves and code, keeps the changes, and commits them.

- Lessons (`learned.md` next to this file, committed): from each task's status ("lesson") and from the brain's
  `learn` tool ("learn X", "remember that ..."). They are part of the system prompt from the next start, and the
  running session gets each new one at its next turn (Harness.notes).
- Moves: motions and skills the brain saves with its suffix (`_own`) live in tonyd's shared user dir; after a task
  that created some they are mirrored into the repo (`data/learned_own/`) and committed.
- Code: `improve_code(request)` runs a separate Codex coding session in this worktree (sandbox: workspace-write,
  edits limited to the harness's own files). The harness then runs the tests itself: green -> commit, then a hot
  reload (the process re-execs itself as soon as it is idle); red -> the changes are reverted.
Every change is a commit on this branch, so `git log` is the record of what Tony taught himself.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from tonypi.harness.peripherals import Ctx, Peripheral, Result, Tool

log = logging.getLogger(__name__)

REPO = Path(__file__).resolve().parents[3]
LESSONS = Path(__file__).with_name("learned.md")
MAX_LESSONS = 40
USER_DIR = Path(os.environ.get("TONYPI_USER_DIR", Path.home() / ".local/share/tonypi"))
SUFFIX = os.environ.get("TONYPI_MOVE_SUFFIX", "_own") or "_own"
EXPORT = REPO / "data" / "learned_own"
CODE_PATHS = ["src/tonypi/harness", "tests/test_harness.py", "tests/fakes", "tools/harness_bench.py"]
TEST_CMD = [sys.executable, "-m", "pytest", "-q", "-x", "tests/test_harness.py", "tests/test_tonyd.py"]
DEV_MODEL = os.environ.get("TONYPI_DEV_MODEL", "gpt-6.1-sol")
DEV_TIMEOUT_S = 600

DEV_PROMPT = """You are improving the code of Tony's brain harness (a Hiwonder TonyPi humanoid robot), in the git \
worktree at {repo}. Request from Tony's brain (it may come from the owner): {request}

Rules: edit only these paths: {paths}. Never touch src/tonypi/tonyd, src/tonypi/mcp_server.py, src/tonypi/agent or \
provisioning. Read src/tonypi/harness/core.py and the module you change first; keep the surrounding style (short \
comments, same naming). Keep the change small and focused on the request. Add or update a test in \
tests/test_harness.py when behaviour changes. Run `{python} -m pytest -q -x tests/test_harness.py` and make it pass. \
Don't commit (the harness does). End with one paragraph: what you changed and why."""


def _similar(a: str, b: str) -> bool:
    """The same lesson in other words (the brain often saves it via learn() and again in the turn's "lesson")."""
    wa, wb = (set("".join(c for c in x.lower() if c.isalnum() or c.isspace()).split()) for x in (a, b))
    return bool(wa and wb) and len(wa & wb) / len(wa | wb) >= 0.6


def git(*args: str, check: bool = True) -> str:
    return subprocess.run(["git", *args], cwd=REPO, capture_output=True, text=True, check=check).stdout.strip()


def status() -> list[tuple[str, str]]:
    """(status code, path) per changed file (git status --porcelain, not stripped: the code column matters)."""
    out = subprocess.run(["git", "status", "--porcelain", "-uall"], cwd=REPO, capture_output=True, text=True,
                         check=True).stdout
    return [(ln[:2], ln[3:]) for ln in out.splitlines() if ln.strip()]


def commit(paths: list[str], message: str) -> str | None:
    """Commit the given paths (if anything changed); returns the short hash."""
    git("add", "-A", "--", *[p for p in paths if (REPO / p).exists() or git("ls-files", "--", p)])
    if not git("diff", "--cached", "--name-only"):
        return None
    git("commit", "-q", "-m", message + "\n\nCo-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>")
    return git("rev-parse", "--short", "HEAD")


class Learning(Peripheral):
    name = "learning"
    prompt = (
        "Learning and self-update: you get better over time and your changes persist. learn(lesson) saves a short "
        "reusable lesson into your own instructions (also do this when told 'learn X' or 'remember Y'). New moves "
        "you author are saved with save_motion/save_skill under the suffix '%s' and kept. improve_code(request) "
        "changes the harness's own Python code (a coding agent edits it, tests must pass, it is committed and "
        "reloaded): use it when told 'improve/fix how you do X' and the fix is in your tools or harness, or by "
        "yourself after a failure caused by a missing or broken tool. Lessons in 'Lessons learned' below come from "
        "earlier tasks." % SUFFIX)

    def __init__(self) -> None:
        self.harness = None
        self.reload_pending = False

    def attach(self, harness) -> None:
        self.harness = harness
        harness.on_lesson = self.add_lesson
        harness.extra_prompt = self.lessons_prompt
        harness.on_task_end = self.after_task

    # ---- lessons ------------------------------------------------------------------------------------------------

    def lessons(self) -> list[str]:
        if not LESSONS.exists():
            return []
        return [ln[2:].strip() for ln in LESSONS.read_text().splitlines() if ln.startswith("- ")]

    def lessons_prompt(self) -> str:
        ls = self.lessons()
        return ("Lessons learned (from earlier tasks; follow them):\n" + "\n".join(f"- {x}" for x in ls)) if ls else ""

    def add_lesson(self, lesson: str, task: str = "") -> bool:
        lesson = " ".join(lesson.split())[:300]
        ls = self.lessons()
        if not lesson or any(_similar(lesson, x) for x in ls):
            return False
        ls = (ls + [lesson])[-MAX_LESSONS:]
        LESSONS.write_text("# Lessons Tony learned (harness-own; written by the harness, one per line)\n\n"
                           + "".join(f"- {x}\n" for x in ls))
        if self.harness:
            self.harness.notes.append(f"New lesson saved to your instructions: {lesson}")
        h = commit([str(LESSONS.relative_to(REPO))], f"harness-own learned: {lesson[:60]}")
        log.info("lesson saved (%s): %s", h, lesson)
        return True

    # ---- moves --------------------------------------------------------------------------------------------------

    def export_moves(self) -> str | None:
        """Mirror the brain's own motions/skills (suffix) from tonyd's user dir into the repo and commit them."""
        motions = sorted((USER_DIR / "motions").glob(f"*{SUFFIX}.json"))
        skills_file = USER_DIR / "skills.json"
        skills = {k: v for k, v in (json.loads(skills_file.read_text()) if skills_file.exists() else {}).items()
                  if k.endswith(SUFFIX)}
        if not motions and not skills:
            return None
        (EXPORT / "motions").mkdir(parents=True, exist_ok=True)
        for m in motions:
            shutil.copy2(m, EXPORT / "motions" / m.name)
        (EXPORT / "skills.json").write_text(json.dumps(skills, indent=1, sort_keys=True) + "\n")
        return commit([str(EXPORT.relative_to(REPO))], f"harness-own learned moves: {len(motions)} motions, "
                                                        f"{len(skills)} skills")

    def after_task(self, record: dict) -> None:
        if record.get("created"):
            try:
                h = self.export_moves()
                if h:
                    log.info("learned moves committed (%s)", h)
            except Exception:
                log.exception("export of learned moves failed")

    # ---- code ---------------------------------------------------------------------------------------------------

    def improve_code(self, args: dict, ctx: Ctx) -> Result:
        request = str(args.get("request", "")).strip()
        if len(request) < 10:
            return Result("say what to improve and why (request)", error=True)
        inside = lambda path: any(path == c or path.startswith(c.rstrip("/") + "/") for c in CODE_PATHS)  # noqa: E731
        before = set(status())  # other files with changes already (bench logs): never touched below
        if dirty := [p for _, p in before if inside(p)]:
            return Result(f"refused: the harness code has uncommitted changes, can't self-edit safely: {dirty[:10]}",
                          error=True)
        ctx.emit("info", text=f"improve_code: {request}")
        t = time.monotonic()
        prompt = DEV_PROMPT.format(repo=REPO, request=request, paths=", ".join(CODE_PATHS), python=sys.executable)
        cmd = [shutil.which("codex") or str(Path.home() / ".local/bin/codex"), "exec", "--sandbox", "workspace-write",
               "-c", f'model="{DEV_MODEL}"', "-c", 'model_reasoning_effort="medium"', "--skip-git-repo-check", prompt]
        if os.environ.get("TONYPI_DEV_CMD"):  # tests: a fake coding agent
            cmd = os.environ["TONYPI_DEV_CMD"].split() + [prompt]
        env = {**os.environ, "CODEX_HOME": os.environ.get("TONYPI_CODEX_HOME", "/home/pi/.config/tonypi/codex-own")}
        try:
            dev = subprocess.run(cmd, cwd=REPO, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                 timeout=DEV_TIMEOUT_S)
            report = dev.stdout.strip().splitlines()[-12:]
        except subprocess.TimeoutExpired:
            report = ["(the coding session ran out of time)"]
        changed = [p for code, p in status() if (code, p) not in before]
        outside = [p for p in changed if not inside(p)]
        if not changed:
            return Result("improve_code: no change was made. Coding session said: " + " ".join(report)[-800:])
        tests = subprocess.run(TEST_CMD, cwd=REPO, capture_output=True, text=True, timeout=300)
        ok = tests.returncode == 0 and not outside
        if not ok:
            if tracked := [c for c in CODE_PATHS if git("ls-files", "--", c)]:
                git("checkout", "-q", "HEAD", "--", *tracked)
            git("clean", "-qfd", "--", *CODE_PATHS, check=False)
            for p in outside:  # what the coding agent changed elsewhere goes back too
                if git("ls-files", "--", p):
                    git("checkout", "-q", "HEAD", "--", p)
                else:
                    (REPO / p).unlink(missing_ok=True)
            last = (tests.stdout.strip().splitlines() or ["tests failed"])[-1]
            why = f"it touched files outside the harness: {', '.join(outside)}" if outside else f"tests failed: {last}"
            return Result(f"improve_code: reverted ({why}). Coding session said: " + " ".join(report)[-600:],
                          error=True)
        h = commit(CODE_PATHS, f"harness-own self-update: {request[:70]}")
        self.reload_pending = True
        ctx.emit("info", text=f"self-update committed ({h}); reloading when idle")
        return Result(f"improve_code: done in {time.monotonic() - t:.0f} s, tests pass, committed {h}; the harness "
                      "reloads with the new code as soon as this task ends. Coding session said: "
                      + " ".join(report)[-600:])

    def learn_tool(self, args: dict, ctx: Ctx) -> Result:
        lesson = str(args.get("lesson", ""))
        saved = self.add_lesson(lesson)
        return Result("saved to your instructions and committed" if saved else "already known (or empty)")

    def tools(self) -> list[Tool]:
        return [
            Tool("learn", "Save a short reusable lesson into your own instructions (persists, committed).",
                 self.learn_tool, {"lesson": {"type": "string"}}, ["lesson"]),
            Tool("lessons", "Your saved lessons.", lambda a, c: Result("\n".join(self.lessons()) or "(none yet)"),
                 read_only=True),
            Tool("improve_code", "Change the harness's own code (a coding agent edits src/tonypi/harness; tests must "
                 "pass; committed; reloaded after this task). Slow (minutes): say what to change and why.",
                 self.improve_code, {"request": {"type": "string"}}, ["request"]),
        ]


def reexec() -> None:
    """Hot reload: replace this process with a fresh copy (same arguments): new code, new instructions."""
    log.warning("reloading the harness with its new code")
    os.execv(sys.executable, [sys.executable, *sys.argv])
