"""Live mode: GPT-Live-1 is Tony's voice and its own Codex agent ("L") is Tony's brain, with full access.

Unlike the relay mode in service.py (GPT-Live -> our harness), here the Codex app-server itself is the delegate
(clientManagedHandoffs: false): every spoken request becomes a turn, or a steer of the running turn, on one persistent
Codex thread L that has a shell, file edits, web search, image viewing, subagents and the robot MCP (tonypi-mcp).
Codex forwards L's [FINAL] messages to GPT-Live to speak and its [COMMENTARY]/reasoning as quiet context
(codexResponseHandoffMode "bemTags", backendReasoningStatus). Codex wraps each request with the transcript since the
previous one, so L sees the whole conversation; L is resumed across restarts and seeded via initialItems.

The voice service adds what Codex can't: tony_* dynamic tools (speak now, background worker threads for concurrent
jobs, switch brains, restart its own voice), the conversation log, and the reflexes (stop -> tonyd stop + latch, without
any model). Design and evidence: docs/LIVE_MODE_SPEC.md (phase 1 of it is implemented here).
"""

from __future__ import annotations

import asyncio
import hashlib
import itertools
import json
import logging
import os
import subprocess
import time
from pathlib import Path

from tonypi.voice.service import DUPLEX, VOICE, Live

log = logging.getLogger("tonypi.voice.live")

CODEX_HOME_LIVE = os.environ.get("TONYPI_LIVE_CODEX_HOME", "/home/pi/.config/tonypi/codex-live")
DATA = Path(os.environ.get("TONYPI_LIVE_DIR", "/home/pi/.local/share/tonypi/live"))
PROMPTS = Path(__file__).parent / "prompts"
REPO = "/home/pi/tonypi"
MODEL = os.environ.get("TONYPI_LIVE_MODEL", "gpt-6.1-terra")
# Codex calls Fast mode "fast" in config and sends it as the priority service tier.
SERVICE_TIER = os.environ.get("TONYPI_LIVE_SERVICE_TIER", "fast")
MAX_WORKERS = 3
PROMPT_MODULES = os.environ.get("TONYPI_LIVE_PROMPT_MODULES", "m1_style,m2_language,m3_unclear,m4_pronounce,m5_reuse")


PROMPT_VARIANT = os.environ.get("TONYPI_LIVE_PROMPT", "legacy")


def live_prompt() -> str:
    """GPT-Live's prompt; the only text kept verbatim through its context replacement. Variants (TONYPI_LIVE_PROMPT):
    legacy = the short relay prompt that delegated reliably in live tests (default: the spec's core prompt, worded
    "Generate <|SpawnThinking|>", made gpt-live-1-codex answer "Okay, waving now." itself without handing off);
    plain = core + modules with that wording replaced; core = the spec's prompt as written (docs/LIVE_MODE_SPEC.md)."""
    if PROMPT_VARIANT == "legacy":
        from tonypi.voice.service import LIVE_PROMPT

        return LIVE_PROMPT
    core = (PROMPTS / "live_core.md").read_text()
    if PROMPT_VARIANT == "plain":
        core = (core.replace("Generate <|SpawnThinking|>", "Hand it to your brain")
                    .replace("Do not generate <|SpawnThinking|>", "Do not hand it to your brain")
                    .replace("only <|SpawnThinking|> does", "only a handoff does").replace("SpawnThinking, ", ""))
    note = ("(Your microphone turns off while you talk, so the user can't interrupt you by voice and any sound you make "
            "cuts off their words. Keep each turn short.)") if not DUPLEX else ""
    start = core.find("{DUPLEX_NOTE")
    if start >= 0:
        core = core[:start] + note + core[core.index("}", start) + 1:]
    parts = [core.strip()]
    for m in [x.strip() for x in PROMPT_MODULES.split(",") if x.strip()]:
        if (f := PROMPTS / f"live_{m}.md").exists():
            parts.append(f.read_text().strip())
    return "\n\n".join(parts)


def _text(name: str) -> str:
    return (PROMPTS / name).read_text().strip()


def _fn(name: str, description: str, props: dict, required: list[str]) -> dict:
    return {"type": "function", "name": name, "description": description,
            "inputSchema": {"type": "object", "properties": props, "required": required, "additionalProperties": False}}


DYNAMIC_TOOLS = [
    _fn("tony_speak_now", "Make Tony say something aloud now, before your turn ends: a short progress update the user "
        "should hear during long work, or a warning. One or two short spoken sentences. Not for the final answer (that "
        "is your [FINAL] message).", {"text": {"type": "string"}}, ["text"]),
    _fn("tony_start_task", "Start a background Codex worker for independent work longer than ~20 s (code changes, "
        "tests, research, long calibration, gait tuning). Returns a task id at once; the result is spoken to the user "
        "and put into this thread when it finishes. The brief must be self-contained.",
        {"title": {"type": "string", "description": "3-6 words, spoken to the user"},
         "brief": {"type": "string", "description": "Self-contained: goal, the user's exact words, constraints, "
                   "decisions made, files, how to verify, what to report"},
         "uses_body": {"type": "boolean", "description": "true if the worker will move the robot or use the head "
                       "camera; then you must not move the body yourself until it finishes"},
         "full_context": {"type": "boolean", "description": "fork this thread (whole conversation) instead of brief + "
                          "recent transcript; expensive"},
         "effort": {"type": "string", "enum": ["low", "medium", "high", "xhigh"]}},
        ["title", "brief", "uses_body"]),
    _fn("tony_tasks", "List background tasks: id, title, status (running/completed/failed/interrupted), age, result "
        "if done.", {}, []),
    _fn("tony_steer_task", "Send a correction or new detail to a running background task.",
        {"task_id": {"type": "string"}, "text": {"type": "string"}}, ["task_id", "text"]),
    _fn("tony_cancel_task", "Interrupt a running background task (task_id='all' for every task). Returns the "
        "confirmed status.", {"task_id": {"type": "string"}}, ["task_id"]),
    _fn("tony_switch_brain", "Switch Tony to another brain (pi, hermes, own, local). The voice service announces it "
        "and switches; reply only with a [COMMENTARY] line afterwards.",
        {"name": {"type": "string", "enum": ["pi", "hermes", "own", "local"]}}, ["name"]),
    _fn("tony_restart_voice", "Restart Tony's voice service (which also runs this Codex app-server) to load changed "
        "voice code, prompts or Codex config. Commit first. It runs checks (imports, voice tests) and restarts after "
        "your current turn ends; this thread is resumed (or replaced if Tony's tools changed).",
        {"reason": {"type": "string"},
         "force": {"type": "boolean", "description": "restart even though background tasks are running (they are lost)"}},
        ["reason"]),
]
TOOLS_HASH = hashlib.sha256(json.dumps(DYNAMIC_TOOLS, sort_keys=True).encode()).hexdigest()[:16]

WORKER_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["status", "spoken_sentence", "details", "commits", "needs_voice_restart", "question"],
    "properties": {
        "status": {"type": "string", "enum": ["done", "failed", "partial", "needs_input"]},
        "spoken_sentence": {"type": "string", "description": "one plain spoken sentence: outcome + key fact"},
        "details": {"type": "string", "description": "what changed (files, commits), how verified, what is left"},
        "commits": {"type": "array", "items": {"type": "string"}},
        "needs_voice_restart": {"type": "boolean"},
        "question": {"type": ["string", "null"], "description": "a question for the user if status is needs_input"},
    },
}


class Conversation:
    """Append-only log of what was said (voice, typed, results): seeds GPT-Live after restarts and briefs workers."""

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)

    def add(self, role: str, text: str, **kw) -> None:
        text = (text or "").strip()
        if not text:
            return
        with self.path.open("a") as f:
            f.write(json.dumps({"t": round(time.time(), 1), "role": role, "text": text, **kw}) + "\n")
        if self.path.stat().st_size > 5_000_000:
            self.path.rename(self.path.with_suffix(".old.jsonl"))

    def tail(self, n: int) -> list[dict]:
        if not self.path.exists():
            return []
        lines = self.path.read_text().splitlines()[-n:]
        return [json.loads(x) for x in lines if x.strip()]


# How a request that didn't come through GPT-Live reached the brain (voice.sock "text" op, by its source).
SOURCE_LABELS = {
    "phone_mic": "Source: heard by Tony's iPhone microphone (speech recognition, may contain mistakes), not by the voice",
    "phone_app": "Source: typed in Tony's iPhone app (not spoken)",
}


class LiveBrain(Live):
    def __init__(self, hub) -> None:
        super().__init__(hub)
        self.codex_home = CODEX_HOME_LIVE
        self.state_path = DATA / "state.json"
        self.convo = Conversation(DATA / "conversation.jsonl")
        self.l_turn: str | None = None  # L's active turn id
        self.workers: dict[str, dict] = {}  # task id -> {thread, turn, title, status, started, uses_body, result}
        self.by_thread: dict[str, str] = {}  # worker thread id -> task id
        self.last_message: dict[str, str] = {}  # thread id -> last agentMessage text
        self.task_ids = itertools.count(1)
        self.restart_after_turn: str | None = None
        # Codex's own link to the live call (its "sideband") sometimes misses a delegation, or its turn stalls; our data
        # channel sees every delegation, so we bridge: hand the request to L ourselves and speak L's answer.
        self.codex_took: float = 0.0  # last time Codex reported a handoff_request
        self.bridged: dict[str, str] = {}  # L turn id -> delegation item id we answer ourselves
        self.l_turn_started = self.l_last_item = 0.0
        self.l_turn_input = ""
        self.last_delegation: tuple[str, str] | None = None
        self.last_delegation_t = 0.0
        self._utt = {"user": [], "assistant": []}

    # ---- thread L ------------------------------------------------------------------------------------------

    def _load_state(self) -> dict:
        try:
            return json.loads(self.state_path.read_text())
        except (OSError, ValueError):
            return {}

    def _save_state(self, **kw) -> None:
        st = {**self._load_state(), **kw}
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(st, indent=1))

    def _l_settings(self) -> dict:
        return {"cwd": REPO, "model": MODEL, "sandbox": "danger-full-access", "approvalPolicy": "never",
                "developerInstructions": _text("codex_developer.md"),
                "serviceTier": SERVICE_TIER,
                "config": {"model_reasoning_effort": "low", "model_reasoning_summary": "concise",
                           "service_tier": SERVICE_TIER}}

    async def setup_thread(self) -> str:
        st = self._load_state()
        if st.get("thread") and st.get("tools_hash") == TOOLS_HASH:
            try:
                r = await self._rpc("thread/resume", {"threadId": st["thread"], "excludeTurns": True,
                                                      **self._l_settings()}, timeout=60)
                log.info("brain thread resumed: %s", st["thread"])
                return r["thread"]["id"]
            except Exception as e:
                log.warning("resume of %s failed (%s): new brain thread", st.get("thread"), e)
        r = await self._rpc("thread/start", {**self._l_settings(), "ephemeral": False, "serviceName": "tonypi-live",
                                             "dynamicTools": DYNAMIC_TOOLS}, timeout=60)
        tid = r["thread"]["id"]
        if st.get("thread"):
            await self._inject(tid, f"Continuity note: you replace an earlier brain thread ({st['thread']}) because "
                               "Tony's tools changed. Read /home/pi/.local/share/tonypi/live/notes.md for durable notes, "
                               "and /home/pi/.local/share/tonypi/live/conversation.jsonl for the recent conversation.")
        self._save_state(thread=tid, tools_hash=TOOLS_HASH, created=time.strftime("%Y-%m-%dT%H:%M:%S"))
        log.info("new brain thread: %s", tid)
        return tid

    def _initial_items(self) -> list[dict]:
        note = [f"Session context from Tony's voice service ({time.strftime('%a %d %b %H:%M')})."]
        try:
            from tonypi.tonyd.client import TonydClient

            s = TonydClient().state()
            # no battery or other changing numbers here: GPT-Live would answer from this note instead of checking
            note.append(f"At session start Tony was {'lying down' if s['lying'] else 'upright'}, camera "
                        f"{'ok' if s['camera']['ok'] else 'missing'}. For battery, posture, what's in view or anything "
                        "else that changes, always hand off to the brain to check.")
        except Exception:
            pass
        reason = self._load_state().get("restart_reason")
        if reason:
            note.append(f"The voice just restarted ({reason}). Greet the user in a few words, e.g. \"I'm back.\"")
            self._save_state(restart_reason=None)
        # Earlier turns go in as one quiet developer note, NOT as user/assistant items: seeded as real turns, GPT-Live
        # imitated Tony's old replies ("Okay, waving now.") and stopped handing requests to the brain (2026-10-30).
        turns = [t for t in self.convo.tail(40) if t["role"] in ("user", "assistant")][-16:]
        recent = " | ".join(f"{'User' if t['role'] == 'user' else 'Tony'}: {t['text'][:200]}" for t in turns)[-3000:]
        if recent:
            note.append(f"Earlier conversation, for reference only (still hand every request to the brain): {recent}")
        return [{"role": "developer", "text": " ".join(note)}]

    def realtime_params(self, sdp: str) -> dict:
        return {
            "threadId": self.tid, "outputModality": "audio", "version": "v3",
            "transport": {"type": "webrtc", "sdp": sdp}, "voice": VOICE, "prompt": live_prompt(),
            "includeStartupContext": False, "initialItems": self._initial_items(),
            "clientManagedHandoffs": False, "codexResponseHandoffMode": "bemTags", "backendReasoningStatus": True,
            "delegationAckFiller": False,
            "flushTranscriptTailOnSessionEnd": True,
            "realtimeStartInstructions": _text("codex_realtime_start.md"),
            "realtimeEndInstructions": _text("codex_realtime_end.md"),
            "voiceSessionId": f"tonypi-live-{int(time.time())}",
        }

    async def _inject(self, thread: str, text: str) -> None:
        try:
            await self._rpc("thread/inject_items", {"threadId": thread, "items": [
                {"type": "message", "role": "developer", "content": [{"type": "input_text", "text": text}]}]})
        except Exception as e:
            log.warning("inject into %s failed: %s", thread, e)

    # ---- typed input and reflex hooks from the Hub --------------------------------------------------------

    async def send_text(self, text: str, source: str = "typed") -> dict:
        """A typed request: a new turn on L, or a steer of the running one (the same as a spoken correction)."""
        self.convo.add("user", text, source=source)
        inp = [{"type": "text", "text": text}]
        label = SOURCE_LABELS.get(source, f"Source: {source} in the app (not spoken)")
        ctx = {"source": {"kind": "application", "value": label}}
        if self.l_turn:
            try:
                await self._rpc("turn/steer", {"threadId": self.tid, "expectedTurnId": self.l_turn, "input": inp})
                return {"steered": self.l_turn}
            except Exception as e:
                log.info("steer failed (%s): starting a turn", e)
        r = await self._rpc("turn/start", {"threadId": self.tid, "input": inp, "additionalContext": ctx})
        return {"turn": (r.get("turn") or {}).get("id")}

    async def interrupt_all(self, body_only: bool = False) -> None:
        """The stop reflex: interrupt L's turn and body tasks (tonyd was already stopped and latched)."""
        if self.l_turn:
            try:
                await self._rpc("turn/interrupt", {"threadId": self.tid, "turnId": self.l_turn}, timeout=5)
            except Exception as e:
                log.warning("interrupt L: %s", e)
        for task_id, w in self.workers.items():
            if w["status"] == "running" and (w["uses_body"] or not body_only):
                await self._cancel(task_id)
        await self._inject(self.tid, f"User said stop at {time.strftime('%H:%M:%S')}: the voice service stopped "
                           "tonyd (stop latch on) and interrupted running body work. Don't move until asked.")

    # ---- data channel + notifications -------------------------------------------------------------------

    def on_dc_event(self, ev: dict) -> bool:
        typ = ev.get("type", "")
        item = ev.get("item") or {}
        if typ == "delegation.created":  # Codex should take it; we log, run the switch reflex, and check it did
            text = " ".join(c.get("text", "") for c in item.get("content", []) if isinstance(c, dict)).strip()
            log.info("delegation: %r", text)
            self.last_delegation = (item.get("id", ""), text)
            self.last_delegation_t = time.monotonic()
            self.hub.on_live_delegation(text)
            asyncio.get_running_loop().call_later(3.0, lambda: asyncio.create_task(
                self._ensure_taken(item.get("id", ""), text, time.monotonic() - 3.0)))
            return True
        if typ == "input_transcript.added":
            self._utt["user"].append(item.get("text", ""))
            return False  # the Hub's transcript reflexes (stop, switch) still run
        if typ == "output_transcript.added":
            self._utt["assistant"].append(item.get("text", ""))
            return True
        if typ == "turn.done":
            turn = ev.get("turn") or {}
            role, text = turn.get("role"), turn.get("transcript")
            if role in ("user", "assistant") and text:
                self._utt[role] = []
                self.convo.add(role, text, source="voice")
            else:
                self._flush_utterances()
            return True
        if typ == "output_audio_buffer.cleared":
            self._flush_utterances()
        return False

    def _flush_utterances(self) -> None:
        for role in ("user", "assistant"):
            if self._utt[role]:
                self.convo.add(role, " ".join("".join(self._utt[role]).split()), source="voice")
                self._utt[role] = []

    async def _ensure_taken(self, item_id: str, text: str, since: float) -> None:
        if self.codex_took >= since or not text:
            return
        log.warning("Codex didn't take delegation %r: bridging it", text)
        await self._bridge(item_id, text)

    def utterance_done(self, text: str) -> None:
        """Called by the Hub when a user utterance ends. GPT-Live sometimes answers without handing off ("Okay,
        marching" and nothing moves): if no delegation follows within 3 s, the brain gets it from us."""
        end = time.monotonic()

        async def check() -> None:
            await asyncio.sleep(3.0)
            if self.last_delegation_t >= end - 1.5:  # GPT-Live delegated it (possibly just before the pause)
                return
            log.warning("GPT-Live didn't hand off %r: bridging it", text)
            self.convo.add("user", text, source="voice (bridged)")
            await self._bridge("", text)
        asyncio.create_task(check())

    async def _bridge(self, item_id: str, text: str) -> None:
        """Hand a spoken request to L ourselves (turn or steer); its [FINAL] is appended to the delegation by us."""
        msg = f"<realtime_delegation>\n  <input>{text}</input>\n  <transcript_delta>user: {text}</transcript_delta>\n</realtime_delegation>"
        inp = [{"type": "text", "text": msg}]
        if self.l_turn:
            try:
                await self._rpc("turn/steer", {"threadId": self.tid, "expectedTurnId": self.l_turn, "input": inp})
                self.bridged[self.l_turn] = item_id
                return
            except Exception as e:
                log.info("bridge steer failed (%s): new turn", e)
        r = await self._rpc("turn/start", {"threadId": self.tid, "input": inp})
        turn = (r.get("turn") or {}).get("id")
        if turn:
            self.bridged[turn] = item_id

    async def watchdog(self) -> None:
        """A turn with no progress for 30 s is stuck (seen when Codex's link to the call dropped): interrupt, re-run."""
        while not self.closed.is_set():
            await asyncio.sleep(5)
            if self.l_turn and time.monotonic() - max(self.l_turn_started, self.l_last_item) > 30:
                stuck, self.l_turn = self.l_turn, None
                log.warning("brain turn %s stuck for 30 s: interrupting and re-running", stuck)
                try:
                    await self._rpc("turn/interrupt", {"threadId": self.tid, "turnId": stuck}, timeout=10)
                except Exception as e:
                    log.warning("interrupt failed: %s", e)
                if self.last_delegation:
                    await self._bridge(*self.last_delegation)

    def on_notification(self, method: str, params: dict) -> None:
        tid = params.get("threadId")
        if tid == self.tid and method.startswith("item/"):
            self.l_last_item = time.monotonic()
        if method == "thread/realtime/itemAdded" and (params.get("item") or {}).get("type") == "handoff_request":
            self.codex_took = time.monotonic()
        if method.startswith(("turn/", "item/started", "item/completed", "thread/realtime/item")) or "error" in method:
            it = params.get("item") or {}
            log.info("codex %s %s %s", method, "L" if tid == self.tid else self.by_thread.get(tid, tid),
                     it.get("type", "") or json.dumps(params)[:160])
        if method == "turn/started" and tid == self.tid:
            self.l_turn = (params.get("turn") or {}).get("id")
            self.l_turn_started = time.monotonic()
        elif method == "item/completed":
            it = params.get("item") or {}
            if it.get("type") == "agentMessage":
                self.last_message[tid] = it.get("text") or ""
                if tid == self.tid:
                    log.info("brain: %s", (it.get("text") or "")[:300])
                    item_id = self.bridged.get(params.get("turnId", ""))
                    if item_id is not None:  # we bridged this request, so we deliver the answer ourselves
                        text = (it.get("text") or "").strip()
                        quiet = text.startswith(("[COMMENTARY]", "[ANALYSIS]", "[STATUS]"))
                        clean = text.split("]", 1)[1].strip() if text.startswith("[") and "]" in text[:16] else text
                        if clean and item_id:
                            self.append(item_id, clean, speak=not quiet)
                        elif clean and not quiet:  # no delegation to answer: speak it as Tony
                            self.speak(clean)
        elif method == "turn/completed":
            if tid == self.tid:
                self.l_turn = None
                self.bridged.pop((params.get("turn") or {}).get("id", ""), None)
                self._flush_utterances()
                if self.restart_after_turn:
                    asyncio.create_task(self._do_restart())
            elif tid in self.by_thread:
                asyncio.create_task(self._worker_done(self.by_thread[tid], params.get("turn") or {}))

    async def on_server_request(self, method: str, params: dict) -> dict:
        if method == "item/tool/call":
            if params.get("threadId") != self.tid:
                return {"contentItems": [{"type": "inputText", "text": "not available here"}], "success": False}
            try:
                text = await self._tool(params.get("tool", ""), params.get("arguments") or {})
                return {"contentItems": [{"type": "inputText", "text": text}], "success": True}
            except Exception as e:
                log.exception("tool %s", params.get("tool"))
                return {"contentItems": [{"type": "inputText", "text": f"error: {e}"}], "success": False}
        if method.endswith("requestApproval"):
            log.warning("approval asked (%s) despite approvalPolicy never: accepting", method)
            return {"decision": "accept"}
        if method in ("applyPatchApproval", "execCommandApproval"):
            return {"decision": "approved"}
        if method == "currentTime/read":
            return {"currentTimeAt": int(time.time())}
        if method in ("mcpServer/elicitation/request", "item/tool/requestUserInput"):
            return {"action": "decline"}
        raise ValueError(f"unsupported: {method}")

    # ---- tony_* tools -----------------------------------------------------------------------------------

    async def _tool(self, name: str, a: dict) -> str:
        if name == "tony_speak_now":
            text = str(a.get("text", ""))[:400]
            ok = self.speak(text)
            self.convo.add("assistant", text, source="speak_now")
            return "spoken" if ok else "voice not ready; nothing said"
        if name == "tony_start_task":
            return await self._start_task(a)
        if name == "tony_tasks":
            if not self.workers:
                return "no background tasks"
            now = time.monotonic()
            return "\n".join(f"{k}: {w['title']} ({w['status']}, {int(now - w['started'])} s)"
                             + (f" -> {w['result'].get('spoken_sentence', '')}" if w.get("result") else "")
                             for k, w in self.workers.items())
        if name == "tony_steer_task":
            w = self.workers.get(a.get("task_id", ""))
            if not w or w["status"] != "running":
                return "no such running task"
            await self._rpc("turn/steer", {"threadId": w["thread"], "expectedTurnId": w["turn"],
                                           "input": [{"type": "text", "text": f"[steer] {a.get('text', '')}"}]})
            return "steered"
        if name == "tony_cancel_task":
            ids = [k for k, w in self.workers.items() if w["status"] == "running"] if a.get("task_id") == "all" \
                else [a.get("task_id", "")]
            return "; ".join([await self._cancel(k) for k in ids]) or "nothing running"
        if name == "tony_switch_brain":
            self.hub.request_brain_switch({"local": "off"}.get(a.get("name", ""), a.get("name", "")))
            return "switch requested; the voice service announces it"
        if name == "tony_restart_voice":
            running = [k for k, w in self.workers.items() if w["status"] == "running"]
            if running and not a.get("force"):
                return f"refused: background tasks still running ({', '.join(running)}); cancel them or pass force"
            last = self._load_state().get("last_restart", 0)
            if time.time() - last < 90:
                return "refused: the voice restarted less than 90 s ago"
            check = await asyncio.to_thread(subprocess.run, [
                f"{REPO}/.venv/bin/python", "-c", "import tonypi.voice.service, tonypi.voice.live; "
                "tonypi.voice.live.live_prompt()"], capture_output=True, text=True, timeout=60, cwd=REPO)
            if check.returncode:
                return f"refused: the new code doesn't import: {check.stderr.strip()[-600:]}"
            tests = await asyncio.to_thread(subprocess.run, [f"{REPO}/.venv/bin/python", "-m", "pytest", "-q",
                                            "tests/test_voice.py"], capture_output=True, text=True, timeout=300, cwd=REPO)
            if tests.returncode:
                return f"refused: tests/test_voice.py fails: {tests.stdout.strip()[-800:]}"
            self.restart_after_turn = str(a.get("reason", ""))
            return "checks passed; the voice restarts when this turn ends"
        raise ValueError(f"unknown tool {name}")

    async def _do_restart(self) -> None:
        reason, self.restart_after_turn = self.restart_after_turn, None
        await asyncio.sleep(4)  # let the final sentence be spoken
        self._save_state(last_restart=time.time(), restart_reason=reason)
        log.warning("restarting the voice service: %s", reason)
        subprocess.Popen(["sudo", "systemctl", "restart", "--no-block", "tonypi-voice"])

    async def _start_task(self, a: dict) -> str:
        running = [w for w in self.workers.values() if w["status"] == "running"]
        if len(running) >= MAX_WORKERS:
            return f"refused: {MAX_WORKERS} tasks already running ({', '.join(w['title'] for w in running)})"
        uses_body = bool(a.get("uses_body"))
        if uses_body and any(w["uses_body"] for w in running):
            return "refused: another background task already uses the body; cancel it first"
        task_id = f"t{next(self.task_ids)}"
        effort = a.get("effort") or "low"
        cfg = {"model_reasoning_effort": effort, "model_reasoning_summary": "concise",
               "features.multi_agent": False, "service_tier": SERVICE_TIER}
        if a.get("full_context"):
            r = await self._rpc("thread/fork", {"threadId": self.tid, "ephemeral": True, "excludeTurns": True,
                                                "model": MODEL, "serviceTier": SERVICE_TIER,
                                                "sandbox": "danger-full-access", "approvalPolicy": "never",
                                                "developerInstructions": _text("codex_worker.md"), "config": cfg},
                                timeout=60)
        else:
            r = await self._rpc("thread/start", {"cwd": REPO, "model": MODEL, "sandbox": "danger-full-access",
                                                 "approvalPolicy": "never", "ephemeral": True,
                                                 "serviceName": "tonypi-live-worker", "serviceTier": SERVICE_TIER,
                                                 "developerInstructions": _text("codex_worker.md"), "config": cfg},
                                timeout=60)
        thread = r["thread"]["id"]
        convo = "\n".join(f"{t['role']}: {t['text'][:300]}" for t in self.convo.tail(30))[-3000:]
        ctx = {"task": {"kind": "application",  # only untrusted entries may carry a name
                        "value": f"Task metadata: id {task_id}; uses_body={uses_body}; requested {time.strftime('%H:%M')} by Tony's main brain"},
               "conversation": {"kind": "untrusted", "name": "recent_conversation_asr_transcript",
                                "value": convo or "(none)"},
               "notes": {"kind": "application", "value": f"Notes file: {DATA / 'notes.md'}"}}
        t = await self._rpc("turn/start", {"threadId": thread, "effort": effort, "outputSchema": WORKER_SCHEMA,
                                           "additionalContext": ctx,
                                           "input": [{"type": "text", "text": f"BACKGROUND TASK {task_id}: {a.get('title', '')}\n{a.get('brief', '')}"}]},
                            timeout=60)
        self.workers[task_id] = {"thread": thread, "turn": (t.get("turn") or {}).get("id"), "title": a.get("title", ""),
                                 "status": "running", "started": time.monotonic(), "uses_body": uses_body}
        self.by_thread[thread] = task_id
        log.info("task %s started (%s, body=%s): %s", task_id, effort, uses_body, a.get("title"))
        return f"started {task_id}; its result will be spoken and put into this thread when done"

    async def _cancel(self, task_id: str) -> str:
        w = self.workers.get(task_id)
        if not w or w["status"] != "running":
            return f"{task_id}: not running"
        try:
            await self._rpc("turn/interrupt", {"threadId": w["thread"], "turnId": w["turn"]}, timeout=10)
        except Exception as e:
            return f"{task_id}: interrupt failed ({e})"
        w["status"] = "interrupted"
        return f"{task_id}: interrupted"

    async def _worker_done(self, task_id: str, turn: dict) -> None:
        w = self.workers[task_id]
        if w["status"] == "running":
            w["status"] = "completed" if turn.get("status") in (None, "completed") else str(turn.get("status"))
        raw = self.last_message.get(w["thread"], "")
        try:
            res = json.loads(raw)
        except ValueError:
            res = {"status": "partial", "spoken_sentence": f"Background task {w['title']} finished.",
                   "details": raw[:1500], "commits": [], "needs_voice_restart": False, "question": None}
        w["result"] = res
        sentence = res.get("spoken_sentence") or f"{w['title']} is done."
        if res.get("status") == "needs_input" and res.get("question"):
            sentence += " " + res["question"]
        log.info("task %s %s: %s", task_id, w["status"], sentence)
        self.convo.add("assistant", sentence, source=f"task {task_id}")
        self.speak(sentence)
        await self._inject(self.tid, f"Background task {task_id} '{w['title']}' finished ({res.get('status')}): "
                           f"{sentence} Details: {str(res.get('details', ''))[:1500]} Commits: {res.get('commits')} "
                           f"needs_voice_restart: {res.get('needs_voice_restart')}. The user already heard the sentence.")
