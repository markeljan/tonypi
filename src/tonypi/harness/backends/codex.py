"""ChatGPT / Codex backend: stub.

TODO(codex): OpenAI allows ChatGPT-subscription login in outside tools. Plan: run `codex app-server` (JSON-RPC over
stdio, logged in once with `codex login --device-auth`), give it the robot tools through the robot MCP server
(`tonypi-mcp`, or a small stdio MCP shim over Harness.call_tool so steering injection works the same), map
`turn/start` / `turn/interrupt` onto run()/cut(), and speak `agentMessage` items. Enable with
TONYPI_BRAIN=codex,local (or claude,codex,local) once it exists.
"""

from __future__ import annotations

from tonypi.harness.core import BrainUnavailable


class CodexBackend:
    name = "codex"
    injects = False

    def configured(self) -> bool:
        return False  # not implemented yet

    def available(self) -> bool:
        return False

    async def start(self, harness) -> None:
        pass

    async def run(self, text: str, source: str) -> None:
        raise BrainUnavailable("the Codex backend is not implemented yet")

    async def cut(self) -> None:
        pass

    def followup_text(self, msgs: list[str]) -> str:
        return msgs[-1]
