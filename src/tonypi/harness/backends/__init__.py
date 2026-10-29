"""Brain backends. Each serves the harness's peripheral tools to one model and runs one task at a time.

Interface (duck-typed):
  name: str; injects: bool          injects = it can take new words inside a tool result mid-turn
  configured() -> bool              set up at all (logged in, enabled); cheap
  available() -> bool               reachable right now (may block ~1 s: called in a thread)
  async start(harness)              warm up (spawn the CLI, open the session)
  async run(text, source)           one turn, returns when the brain is done; raises BrainUnavailable to fall back
  async cut()                       end the running turn now (stop, or steering that no tool picked up)
  followup_text(msgs) -> str        how words that arrived mid-turn are sent as the next message
"""

from __future__ import annotations

import os


def build(order: str | None = None) -> list:
    """TONYPI_BRAIN: comma list in preference order (default claude,codex,local). local is always appended."""
    from tonypi.harness.backends.claude import ClaudeBackend
    from tonypi.harness.backends.codex import CodexBackend
    from tonypi.harness.backends.local import LocalBackend

    makers = {"claude": ClaudeBackend, "codex": CodexBackend, "local": LocalBackend}
    names = [n.strip() for n in (order or os.environ.get("TONYPI_BRAIN", "claude,codex,local")).split(",") if n.strip()]
    if "local" not in names:
        names.append("local")
    names = names[:names.index("local") + 1]  # local never fails, so nothing after it would ever run
    return [makers[n]() for n in names]
