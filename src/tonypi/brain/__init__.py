"""Tony's long-term mind: memory, goals, journal, recall and the learning ledger (docs/BRAIN.md).

Paths are shared by the voice service, the autonomy scheduler, the learning runner and the `tony` CLI. Tests point
TONYPI_BRAIN_DIR at a temporary directory.
"""

from __future__ import annotations

import os
from pathlib import Path

BRAIN_DIR = Path(os.environ.get("TONYPI_BRAIN_DIR", Path.home() / ".local/share/tonypi/brain"))
LEARN_DIR = BRAIN_DIR / "learning"
LIVE_DIR = Path(os.environ.get("TONYPI_LIVE_DIR", Path.home() / ".local/share/tonypi/live"))
JOBS_LOG = Path(os.environ.get("TONYPI_JOBS_LOG", Path.home() / ".local/share/tonypi/jobs.jsonl"))


def preempt_file() -> Path:
    """Holds the wall-clock time the voice service last ended an autonomy session that could use the body (owner spoke,
    pause, time cap, stop). The learning runner (tonypi.learn.runner.Lab) stops a batch that started before it, and
    skips its closing get_up / settle, so a session's trials don't outlive the session. Read lazily (tests set
    TONYPI_LIVE_DIR)."""
    return Path(os.environ.get("TONYPI_LIVE_DIR", str(Path.home() / ".local/share/tonypi/live"))) / "autonomy-preempt"


def mark_preempt(t: float | None = None) -> None:
    import time

    path = preempt_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(f"{time.time() if t is None else t:.3f}\n")
    tmp.replace(path)
