"""ChatGPT backend: OpenAI's Codex app-server (JSON-RPC over stdio), signed in with the owner's ChatGPT plan.

The loop stays ours: the robot tools are handed to Codex as *dynamic tools*, so every tool call comes back to this
process (`item/tool/call`) and runs through Harness.call_tool (steering injection, motion gate, safety). Codex's own
tools (shell, patching, browser, web search, ...) are switched off. New words mid-turn go in with `turn/steer`;
cuts use `turn/interrupt`. One thread lives as long as the process (one long-running session).

Login (once, by the owner; a device code works on a headless Pi). This CODEX_HOME belongs to the harness alone,
because ChatGPT refresh tokens rotate and two tools sharing one would log each other out:
    CODEX_HOME=/home/pi/.config/tonypi/codex-own codex login --device-auth
"""

from __future__ import annotations

import asyncio
import base64
import itertools
import json
import logging
import os
import re
import shutil
import socket
import time
from pathlib import Path

from tonypi.harness.core import BrainUnavailable

log = logging.getLogger(__name__)

CODEX_HOME = Path(os.environ.get("TONYPI_CODEX_HOME", "/home/pi/.config/tonypi/codex-own"))
# Model + reasoning effort per turn, by tier ("model:effort"; the lowest effort these models take is "low"):
#   fast      one simple command ("wave", "what's your battery")
#   multi     several steps in a row ("walk 3 steps then turn left", "look left and tell me if ...")
#   advanced  open-ended plans, hard perception, recovering after a failed step ("find the door", "go get that")
TIERS = {
    "fast": os.environ.get("TONYPI_CODEX_FAST", "gpt-6.1-terra:low"),
    "multi": os.environ.get("TONYPI_CODEX_MULTI", "gpt-6.1-terra:medium"),
    "advanced": os.environ.get("TONYPI_CODEX_ADVANCED", "gpt-6.1-sol:medium"),
}
FORCE_TIER = os.environ.get("TONYPI_CODEX_TIER", "auto")  # auto | fast | multi | advanced
ADVANCED_RE = re.compile(r"\b(find|search|look for|explore|go (to|get|and)|get (me|that|the)|bring|fetch|follow|"
                         r"come (here|to)|toward|towards|until|navigate|figure out|count|read|how many|which|why|"
                         r"try again|it didn'?t work|you failed|you fell)\b", re.I)
MULTI_RE = re.compile(r"\b(then|and|after|before|while|first|next|tell me if|is there|are there)\b|,", re.I)


def tier_for(text: str, failed_before: bool = False) -> str:
    """Which tier a request needs. A turn after a failed step escalates."""
    if FORCE_TIER in TIERS:
        return FORCE_TIER
    if failed_before or ADVANCED_RE.search(text):
        return "advanced"
    return "multi" if MULTI_RE.search(text) else "fast"

FIRST_REPLY_S = float(os.environ.get("TONYPI_CODEX_FIRST_REPLY_S", "25"))
# reachability probe; T7 points it at a dead port to play "no internet"
PROBE = os.environ.get("TONYPI_CODEX_PROBE", "chatgpt.com:443")
STATE_DIR = Path(os.environ.get("TONYPI_STATE_DIR", Path.home() / ".local/state/tonypi/harness"))
DOWN_FOR_S = 30
# Codex's own tools and extras: all off, the robot tools are all it gets
OFF = ["shell_tool", "unified_exec", "unified_exec_tty", "shell_snapshot", "apps", "plugins", "remote_plugin",
       "plugin_discovery_prefetch", "browser_use", "browser_use_external", "in_app_browser", "computer_use",
       "image_generation", "multi_agent", "view_image", "sleep_tool", "goals", "tool_suggest", "skill_search",
       "skill_mcp_dependency_install", "hooks", "workspace_dependencies", "memories"]
# dynamic tools are called through the code-mode host: `codex-code-mode-host` from the same release must sit next to
# the codex binary, or every tool call fails closed
CONFIG = ['web_search="disabled"', "project_doc_max_bytes=0",
          'cli_auth_credentials_store="file"', "check_for_update_on_startup=false"]
FATAL_HTTP = {401, 402, 403}
# Codex reaches these tools only from its JavaScript `exec` tool; without this, the model guesses the result
# shape, prints a photo as base64 text (~15k tokens) and calls snapshot again
EXEC_NOTE = """

Calling tools: inside exec, every tool returns one string: its text, and each photo on its own line as a
data:image/jpeg;base64,... URL. Use exactly this to see photos (never print image data as text):
  const r = await tools.snapshot({});
  for (const l of r.split("\\n")) l.startsWith("data:image") ? image(l) : text(l);
Await tools one after another when order matters (look, then snapshot)."""
TRACE = int(os.environ.get("TONYPI_CODEX_TRACE", "0"))  # log raw incoming lines, cut to this many chars


class CodexBackend:
    name = "codex"
    injects = True

    def __init__(self) -> None:
        self.h = None
        self.proc: asyncio.subprocess.Process | None = None
        self.thread_id: str | None = None
        self.turn_id: str | None = None
        self.ids = itertools.count(1)
        self.waiting: dict[int, asyncio.Future] = {}
        self.turn_done = asyncio.Event()
        self.turn_done.set()
        self.first = asyncio.Event()
        self.turn: dict | None = None
        self.fatal: str | None = None
        self.did_tools = 0
        self.failed_tools = 0  # failed steps (not steering refusals) in the last turn: the next one escalates
        self.tier = "fast"
        self.down_until = 0.0
        self.probe_ok_until = 0.0
        self._lock: asyncio.Lock | None = None

    # ---- availability -----------------------------------------------------------------------------------------

    def configured(self) -> bool:
        return (CODEX_HOME / "auth.json").exists() and bool(self._bin())

    @staticmethod
    def _bin() -> list[str] | str | None:
        if cmd := os.environ.get("TONYPI_CODEX_CMD"):  # tests: a fake app-server
            return cmd
        return shutil.which("codex") or (str(p) if (p := Path.home() / ".local/bin/codex").exists() else None)

    def available(self) -> bool:
        now = time.monotonic()
        if not self.configured() or now < self.down_until:
            return False
        if now < self.probe_ok_until:
            return True
        host, _, port = PROBE.rpartition(":")
        try:
            socket.create_connection((host, int(port)), 1.5).close()
        except OSError as e:
            log.warning("ChatGPT unreachable (%s): %s", PROBE, e)
            self.down_until = now + 10
            return False
        self.probe_ok_until = now + 30
        return True

    def _down(self, why: str) -> None:
        log.warning("codex backend down: %s", why)
        self.down_until = time.monotonic() + DOWN_FOR_S
        self.probe_ok_until = 0

    # ---- process + JSON-RPC -----------------------------------------------------------------------------------

    async def start(self, harness) -> None:
        self.h = harness
        self._lock = asyncio.Lock()
        asyncio.create_task(self._warm())

    async def _warm(self) -> None:
        try:
            if await asyncio.to_thread(self.available):
                await self._connect()
        except Exception as e:
            log.warning("codex warm-up failed: %s", e)

    async def _connect(self) -> None:
        async with self._lock:
            if self.proc is not None and self.proc.returncode is None and self.thread_id:
                return
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            args = self._bin().split() if os.environ.get("TONYPI_CODEX_CMD") else [self._bin(), "app-server"]
            for f in OFF:
                args += ["--disable", f]
            for c in CONFIG:
                args += ["-c", c]
            t = time.monotonic()
            self.proc = await asyncio.create_subprocess_exec(
                *args, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, env={**os.environ, "CODEX_HOME": str(CODEX_HOME)},
                cwd=str(STATE_DIR), limit=64 * 2**20)
            asyncio.create_task(self._read(self.proc))
            asyncio.create_task(self._stderr(self.proc))
            await self.request("initialize", {"clientInfo": {"name": "tonypi-harness", "version": "0.1"},
                                              "capabilities": {"experimentalApi": True}})
            self.notify("initialized")
            acct = await self.request("account/read", {})
            if not acct.get("account"):
                raise BrainUnavailable(f"not logged in (CODEX_HOME={CODEX_HOME})")
            tools = [{"type": "function", "name": t.name, "description": t.description, "inputSchema": t.schema()}
                     for t in self.h.tools.values()]
            params = {"dynamicTools": tools, "baseInstructions": self.h.system_prompt() + EXEC_NOTE,
                      "approvalPolicy": "never",
                      "sandbox": "read-only", "ephemeral": True, "cwd": str(STATE_DIR)}
            params["model"] = TIERS["fast"].split(":")[0]
            r = await self.request("thread/start", params)
            self.thread_id = r["thread"]["id"]
            log.info("codex thread up in %.1f s (tiers %s, plan %s)", time.monotonic() - t, TIERS,
                     acct["account"].get("planType"))

    def _send(self, msg: dict) -> None:
        self.proc.stdin.write((json.dumps(msg) + "\n").encode())

    async def request(self, method: str, params: dict, timeout: float = 30) -> dict:
        rid = next(self.ids)
        fut = asyncio.get_running_loop().create_future()
        self.waiting[rid] = fut
        self._send({"id": rid, "method": method, "params": params})
        try:
            return await asyncio.wait_for(fut, timeout)
        finally:
            self.waiting.pop(rid, None)

    def notify(self, method: str, params: dict | None = None) -> None:
        self._send({"method": method, "params": params or {}})

    async def _stderr(self, proc) -> None:
        async for line in proc.stderr:
            log.debug("codex: %s", line.decode(errors="replace").rstrip())

    async def _read(self, proc) -> None:
        try:
            async for line in proc.stdout:
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if TRACE:
                    log.info("codex <- %s", line.decode(errors="replace").rstrip()[:TRACE])
                if "method" in msg and "id" in msg:
                    asyncio.create_task(self._server_request(msg))
                elif "method" in msg:
                    self._notification(msg["method"], msg.get("params") or {})
                elif (fut := self.waiting.get(msg.get("id"))) and not fut.done():
                    if "error" in msg:
                        fut.set_exception(RuntimeError(msg["error"].get("message", "error")))
                    else:
                        fut.set_result(msg.get("result") or {})
        finally:
            log.warning("codex app-server exited (%s)", proc.returncode)
            if self.proc is proc:
                self.proc, self.thread_id = None, None
            for fut in self.waiting.values():
                if not fut.done():
                    fut.set_exception(BrainUnavailable("codex app-server exited"))
            self.first.set()
            self.turn_done.set()

    async def _server_request(self, msg: dict) -> None:
        method, params = msg["method"], msg.get("params") or {}
        if method == "item/tool/call":
            self.first.set()
            self.did_tools += 1
            res = await self.h.call_tool(params["tool"], params.get("arguments") or {}, inject=True)
            if res.error and not res.text.startswith("NOT done"):
                self.failed_tools += 1
            items = [{"type": "inputText", "text": res.text}]
            items += [{"type": "inputImage", "imageUrl": "data:image/jpeg;base64," + base64.b64encode(i).decode()}
                      for i in res.images]
            self._send({"id": msg["id"], "result": {"contentItems": items, "success": not res.error}})
        else:  # approvals etc.: nothing here should need one (no shell, read-only, approvalPolicy never)
            log.warning("codex asked %s: refused", method)
            self._send({"id": msg["id"], "error": {"code": -32601, "message": "not supported by tonypi"}})

    def _notification(self, method: str, p: dict) -> None:
        if method == "turn/started":
            self.turn_id = p["turn"]["id"]
        elif method == "item/started" and p.get("item", {}).get("type") not in ("userMessage", None):
            self.first.set()
        elif method == "item/completed":
            item = p.get("item", {})
            if item.get("type") == "agentMessage" and item.get("text", "").strip():
                self.h.say(item["text"])
        elif method == "error":
            err = p.get("error") or {}
            info = err.get("codexErrorInfo") or {}
            status = next((v.get("httpStatusCode") for v in info.values() if isinstance(v, dict)), None)
            log.warning("codex error: %s %s", err.get("message"), err.get("additionalDetails") or "")
            if status in FATAL_HTTP or not p.get("willRetry"):
                self.fatal = f"{err.get('message')} ({status or 'no status'})"
                self.first.set()
        elif method == "thread/tokenUsage/updated":
            last = (p.get("tokenUsage") or {}).get("last") or {}
            cached = last.get("cachedInputTokens") or 0  # OpenAI counts cached tokens inside inputTokens
            self.h.emit("usage", brain="codex", input=(last.get("inputTokens") or 0) - cached,
                        output=last.get("outputTokens"), cache_read=cached,
                        reasoning=last.get("reasoningOutputTokens"))
        elif method == "turn/completed":
            self.turn = p.get("turn") or {}
            self.h.emit("info", text=f"codex turn {self.turn.get('status')} in {self.turn.get('durationMs')} ms")
            self.first.set()
            self.turn_done.set()

    # ---- turns ------------------------------------------------------------------------------------------------

    async def run(self, text: str, source: str) -> None:
        try:
            await self._connect()
        except BrainUnavailable:
            self._down("not logged in")
            raise
        except Exception as e:
            self._down(f"start failed: {e}")
            raise BrainUnavailable(f"can't start codex app-server ({e})") from e
        self.turn_done.clear()
        self.first.clear()
        followup = text.startswith("[New words from the user")
        self.tier = self.tier if followup and self.tier == "advanced" else tier_for(text, self.failed_tools > 0)
        model, _, effort = TIERS[self.tier].partition(":")
        self.h.emit("info", text=f"codex tier {self.tier}: {model}, effort {effort or 'default'}")
        self.turn, self.fatal, self.did_tools, self.failed_tools = None, None, 0, 0
        params = {"threadId": self.thread_id, "model": model,
                  "input": [{"type": "text", "text": f"[{source}] {text}" if source == "voice" else text}]}
        if effort:
            params["effort"] = effort
        try:
            r = await self.request("turn/start", params)
        except Exception as e:
            self._down(f"turn/start failed: {e}")
            raise BrainUnavailable(str(e)) from e
        self.turn_id = r["turn"]["id"]
        try:
            await asyncio.wait_for(self.first.wait(), FIRST_REPLY_S)
        except TimeoutError:
            self.fatal = f"no reply within {FIRST_REPLY_S:.0f} s"
        if self.fatal and not self.did_tools:
            self._down(self.fatal)
            await self.cut()
            raise BrainUnavailable(self.fatal)
        await self.turn_done.wait()
        if (self.turn or {}).get("status") == "failed" and not self.did_tools:
            self._down(str((self.turn.get("error") or {}).get("message")))
            raise BrainUnavailable(f"turn failed: {(self.turn.get('error') or {}).get('message')}")

    async def steer(self, msgs: list[str]) -> bool:
        """Words for the running turn (Codex reads them at its next step). False: no turn to steer."""
        if self.turn_done.is_set() or not self.turn_id:
            return False
        try:
            await self.request("turn/steer", {"threadId": self.thread_id, "expectedTurnId": self.turn_id,
                                              "input": [{"type": "text", "text": self.followup_text(msgs)}]}, 5)
            return True
        except Exception as e:
            log.warning("turn/steer failed: %s", e)
            return False

    async def cut(self) -> None:
        if self.proc is None or self.turn_done.is_set() or not self.turn_id:
            return
        try:
            await self.request("turn/interrupt", {"threadId": self.thread_id, "turnId": self.turn_id}, 5)
            await asyncio.wait_for(self.turn_done.wait(), 5)
        except Exception as e:
            log.warning("interrupt failed (%s); restarting app-server", e)
            if self.proc is not None:
                self.proc.kill()

    def followup_text(self, msgs: list[str]) -> str:
        return f"[New words from the user while you were working: {' / '.join(msgs)}]"
