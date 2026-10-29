"""Claude backend: one long-running Claude Code session (Claude Agent SDK) with the robot tools in-process.

Auth is the owner's Claude subscription through the unmodified Claude Code CLI: `claude setup-token` gives a
1-year token, kept as CLAUDE_CODE_OAUTH_TOKEN in /home/pi/.config/tonypi/secrets.env. ANTHROPIC_API_KEY is
removed from the CLI's environment so it can never silently bill an API key.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import shutil
import socket
import time
from pathlib import Path
from urllib.parse import urlparse

from tonypi.harness.core import BrainUnavailable
from tonypi.harness.peripherals import Result

log = logging.getLogger(__name__)

MODEL = os.environ.get("TONYPI_CLAUDE_MODEL", "claude-opus-5-5")
EFFORT = os.environ.get("TONYPI_CLAUDE_EFFORT", "low")  # a robot waiting on its brain: low effort, fast replies
FIRST_REPLY_S = float(os.environ.get("TONYPI_CLAUDE_FIRST_REPLY_S", "25"))  # nothing back by then: fall back
STATE_DIR = Path(os.environ.get("TONYPI_STATE_DIR", Path.home() / ".local/state/tonypi/harness"))
DOWN_FOR_S = 30  # after a failure, don't try again for this long


class ClaudeBackend:
    name = "claude"
    injects = True

    def __init__(self) -> None:
        self.h = None
        self.client = None
        self.session: asyncio.Task | None = None
        self.turn_done = asyncio.Event()
        self.turn_done.set()
        self.first = asyncio.Event()
        self.result = None
        self.down_until = 0.0
        self.probe_ok_until = 0.0
        self.error: str | None = None
        self._connecting: asyncio.Lock | None = None

    # ---- availability -----------------------------------------------------------------------------------------

    def configured(self) -> bool:
        if not os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
            return False
        try:
            import claude_agent_sdk  # noqa: F401
        except ImportError:
            return False
        return True

    def available(self) -> bool:
        now = time.monotonic()
        if not self.configured() or now < self.down_until:
            return False
        if now < self.probe_ok_until:
            return True
        url = urlparse(os.environ.get("ANTHROPIC_BASE_URL") or "https://api.anthropic.com")
        try:
            socket.create_connection((url.hostname, url.port or (443 if url.scheme == "https" else 80)), 1.5).close()
        except OSError as e:
            log.warning("Claude API unreachable: %s", e)
            self.down_until = now + 10
            return False
        self.probe_ok_until = now + 30
        return True

    def _down(self, why: str) -> None:
        log.warning("claude backend down: %s", why)
        self.down_until = time.monotonic() + DOWN_FOR_S
        self.probe_ok_until = 0

    # ---- session ----------------------------------------------------------------------------------------------

    async def start(self, harness) -> None:
        self.h = harness
        self._connecting = asyncio.Lock()
        asyncio.create_task(self._warm())

    async def _warm(self) -> None:
        try:
            if await asyncio.to_thread(self.available):
                await self._connect()
        except Exception as e:
            log.warning("claude warm-up failed: %s", e)

    async def _connect(self) -> None:
        async with self._connecting:
            if self.client is not None:
                return
            from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient, SdkMcpTool, ToolAnnotations, \
                create_sdk_mcp_server

            tools = [SdkMcpTool(t.name, t.description, t.schema(), self._handler(t.name),
                                ToolAnnotations(readOnlyHint=t.read_only)) for t in self.h.tools.values()]
            env = {"ANTHROPIC_API_KEY": "", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
                   "DISABLE_AUTOUPDATER": "1"}
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            cli = shutil.which("claude") or str(Path.home() / ".local/bin/claude")
            opts = ClaudeAgentOptions(
                system_prompt=self.h.system_prompt(),
                mcp_servers={"robot": create_sdk_mcp_server("robot", tools=tools)},
                strict_mcp_config=True,
                tools=[],  # no Bash/Read/Edit...: the robot tools are all it gets
                allowed_tools=[f"mcp__robot__{n}" for n in self.h.tools],
                permission_mode="dontAsk",
                setting_sources=[],
                model=MODEL,
                effort=EFFORT,
                cwd=str(STATE_DIR),
                cli_path=cli if Path(cli).exists() else None,
                env=env,
                stderr=lambda line: log.info("claude: %s", line.rstrip()),
            )
            t = time.monotonic()
            ready: asyncio.Future = asyncio.get_running_loop().create_future()
            self.session = asyncio.create_task(self._session(ClaudeSDKClient(opts), ready))
            await ready
            log.info("claude session up in %.1f s (model %s, effort %s)", time.monotonic() - t, MODEL, EFFORT)

    async def _session(self, client, ready: asyncio.Future) -> None:
        """Owns the client from connect to disconnect (anyio wants both in the same task)."""
        try:
            await client.connect()
        except Exception as e:
            ready.set_exception(e)
            return
        self.client = client
        ready.set_result(None)
        try:
            await self._read(client)
        finally:
            if self.client is client:
                self.client = None
            self.turn_done.set()
            self.first.set()
            try:
                await asyncio.wait_for(client.disconnect(), 5)
            except BaseException:
                pass

    def _handler(self, name: str):
        async def handle(args: dict) -> dict:
            res: Result = await self.h.call_tool(name, args, inject=True)
            content = [{"type": "text", "text": res.text}]
            content += [{"type": "image", "data": base64.b64encode(img).decode(), "mimeType": "image/jpeg"}
                        for img in res.images]
            return {"content": content, "is_error": res.error}
        return handle

    async def _read(self, client) -> None:
        from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock

        try:
            async for m in client.receive_messages():
                if isinstance(m, AssistantMessage):
                    self.first.set()
                    if m.error:
                        self.error = m.error
                    for b in m.content:
                        if isinstance(b, TextBlock) and b.text.strip() and not m.error:
                            self.h.say(b.text)
                elif isinstance(m, ResultMessage):
                    self.result = m
                    u = m.usage or {}
                    self.h.emit("usage", brain="claude", turns=m.num_turns, ms=m.duration_ms,
                                api_ms=m.duration_api_ms, cost_usd=m.total_cost_usd,
                                input=u.get("input_tokens"), output=u.get("output_tokens"),
                                cache_read=u.get("cache_read_input_tokens"),
                                cache_write=u.get("cache_creation_input_tokens"),
                                error=m.is_error, end=m.terminal_reason or m.subtype)
                    self.first.set()
                    self.turn_done.set()
        except Exception as e:
            log.warning("claude session ended: %s", e)

    # ---- turns ------------------------------------------------------------------------------------------------

    async def run(self, text: str, source: str) -> None:
        try:
            await self._connect()
        except Exception as e:
            self._down(f"connect failed: {e}")
            raise BrainUnavailable(f"can't start Claude Code ({e})") from e
        self.turn_done.clear()
        self.first.clear()
        self.result, self.error = None, None
        await self.client.query(f"[{source}] {text}" if source == "voice" else text)
        try:
            await asyncio.wait_for(self.first.wait(), FIRST_REPLY_S)
        except TimeoutError:
            self._down(f"no reply within {FIRST_REPLY_S:.0f} s")
            await self.cut()
            await self._reset()
            raise BrainUnavailable("no reply from Claude") from None
        await self.turn_done.wait()
        r = self.result
        if self.error in ("authentication_failed", "billing_error", "rate_limit") or (
                r and r.is_error and r.api_error_status and not self._aborted(r)):
            self._down(f"API error {self.error or r.api_error_status}")
            if not self._did_anything(r):
                raise BrainUnavailable(f"Claude API error ({self.error or r.api_error_status})")
        if r is None and self.client is None:
            self._down("CLI exited")
            raise BrainUnavailable("Claude Code exited")

    @staticmethod
    def _aborted(r) -> bool:
        return (r.terminal_reason or "").startswith("aborted")

    @staticmethod
    def _did_anything(r) -> bool:
        return r is not None and (r.num_turns or 0) > 1

    async def cut(self) -> None:
        if self.client is None or self.turn_done.is_set():
            return
        try:
            await self.client.interrupt()
            await asyncio.wait_for(self.turn_done.wait(), 5)
        except Exception as e:
            log.warning("interrupt failed (%s); restarting the session", e)
            await self._reset()

    async def _reset(self) -> None:
        """Drop the session (a fresh one starts on the next turn)."""
        self.client = None
        self.turn_done.set()
        if self.session is not None and not self.session.done():
            self.session.cancel()

    def followup_text(self, msgs: list[str]) -> str:
        return f"[New words from the user while you were working: {' / '.join(msgs)}]"
