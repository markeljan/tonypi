"""pi in `--mode rpc` as a child process: JSONL commands on stdin, responses and session events on stdout."""

from __future__ import annotations

import itertools
import json
import logging
import os
import subprocess
import threading
from concurrent.futures import Future
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)

REPO = Path(__file__).resolve().parents[3]
EXTENSION = REPO / "pi" / "extension"
SYSTEM_PROMPT = REPO / "pi" / "SYSTEM.md"
SESSION_DIR = Path(os.environ.get("TONYPI_PI_SESSIONS", "/home/pi/.local/state/tonypi/pi-sessions"))


def pi_command(model: str) -> list[str]:
    extra = [a for p in os.environ.get("TONYPI_PI_EXTRA_EXT", "").split(",") if p for a in ("-e", p)]
    return ["pi", "--mode", "rpc", "--offline", "--model", model, "--thinking", "low",
            "--no-builtin-tools", "--no-context-files", "--no-skills", "--no-extensions", "--no-mcp",
            "--no-prompt-templates", "--no-themes", "-e", str(EXTENSION), *extra,
            "--system-prompt", SYSTEM_PROMPT.read_text(), "--session-dir", str(SESSION_DIR)]


class PiRpc:
    def __init__(self, on_event: Callable[[dict], None], model: str = "tony/brain") -> None:
        self.on_event = on_event
        self.model = model
        self._ids = itertools.count(1)
        self._pending: dict[str, Future] = {}
        self._wlock = threading.Lock()
        self.proc: subprocess.Popen | None = None

    def start(self) -> None:
        SESSION_DIR.mkdir(parents=True, exist_ok=True)
        env = {**os.environ, "TONYPI_FRONTEND": "1"}
        self.proc = subprocess.Popen(pi_command(self.model), cwd=REPO, env=env, stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        threading.Thread(target=self._read, name="pi-stdout", daemon=True).start()
        threading.Thread(target=self._stderr, name="pi-stderr", daemon=True).start()

    def _read(self) -> None:
        assert self.proc and self.proc.stdout
        for line in self.proc.stdout:  # binary readline splits on LF only, as the protocol requires
            try:
                rec = json.loads(line)
            except ValueError:
                log.warning("pi: bad record %r", line[:200])
                continue
            if rec.get("type") == "response":
                fut = self._pending.pop(rec.get("id"), None)
                if fut and not fut.done():
                    fut.set_result(rec)
                continue
            try:
                self.on_event(rec)
            except Exception:
                log.exception("pi event handler failed")
        log.error("pi exited (code %s)", self.proc.wait())
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(RuntimeError("pi exited"))
        self.on_event({"type": "pi_exit", "code": self.proc.returncode})

    def _stderr(self) -> None:
        assert self.proc and self.proc.stderr
        for line in self.proc.stderr:
            log.info("pi: %s", line.decode(errors="replace").rstrip())

    def send(self, type_: str, timeout: float = 30, **kw) -> dict:
        rid = f"r{next(self._ids)}"
        fut: Future = Future()
        self._pending[rid] = fut
        with self._wlock:
            assert self.proc and self.proc.stdin
            self.proc.stdin.write((json.dumps({"id": rid, "type": type_, **kw}) + "\n").encode())
            self.proc.stdin.flush()
        rec = fut.result(timeout)
        if not rec.get("success"):
            raise RuntimeError(f"pi {type_}: {rec.get('error')}")
        return rec.get("data") or {}

    def send_async(self, type_: str, **kw) -> None:
        """Fire and forget (abort waits for idle before pi answers)."""
        threading.Thread(target=lambda: self._quiet(type_, **kw), daemon=True).start()

    def _quiet(self, type_: str, **kw) -> None:
        try:
            self.send(type_, timeout=60, **kw)
        except Exception as e:
            log.warning("pi %s failed: %s", type_, e)

    def close(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.stdin.close()
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
