"""Live mode (the default): GPT-Live-1 is Tony's voice and its own Codex agent ("L") is Tony's brain, with full access.

The Codex app-server itself is GPT-Live's delegate (clientManagedHandoffs: false): every spoken request becomes a turn,
or a steer of the running turn, on one persistent Codex thread L that has a shell, file edits, web search, image
viewing, subagents, the robot MCP (tonypi-mcp) and the phone MCP. Codex forwards L's [FINAL] messages to GPT-Live to
speak and its [COMMENTARY]/reasoning as quiet context (codexResponseHandoffMode "bemTags", backendReasoningStatus).
L's developer instructions carry a snapshot of Tony's memory (tonypi.brain.store); L is rotated to a fresh thread when
it gets old or its context large.

The voice service adds what Codex can't: tony_* dynamic tools (speak now, background worker threads with optional
native goals, switch to the local agent, restart its own voice), autonomy sessions on Tony's goals when nobody talks
to him (the host side of tonypi.voice.autonomy), the conversation and task logs, the bridge for requests GPT-Live or
Codex dropped, and the reflexes (stop -> tonyd stop + latch, without any model). Docs: docs/BRAIN.md.
"""

from __future__ import annotations

import asyncio
import difflib
import hashlib
import json
import logging
import os
import re
import subprocess
import time
from pathlib import Path

from tonypi.brain import BRAIN_DIR, LIVE_DIR
from tonypi.voice.service import DUPLEX, LIVE_PROMPT, VOICE, Live

log = logging.getLogger("tonypi.voice.live")

CODEX_HOME_LIVE = os.environ.get("TONYPI_LIVE_CODEX_HOME", "/home/pi/.config/tonypi/codex-live")
DATA = LIVE_DIR
PROMPTS = Path(__file__).parent / "prompts"
REPO = "/home/pi/tonypi"
MODELS = {"terra": os.environ.get("TONYPI_LIVE_MODEL", "gpt-6.1-terra"), "sol": "gpt-6.1-sol"}
MODEL = MODELS["terra"]  # L, autonomy and default workers; "sol" only when a task asks for tier sol
L_EFFORT = "low"
AUTONOMY_EFFORT = "medium"
# Codex calls Fast mode "fast" in config and sends it as the priority service tier.
SERVICE_TIER = os.environ.get("TONYPI_LIVE_SERVICE_TIER", "fast")
MAX_WORKERS = 3
WORKER_GOAL_TOKENS = 300_000  # token budget of a worker's native goal
L_MAX_AGE_S = 20 * 3600  # rotate L to a new thread when older than this ...
L_MAX_INPUT_TOKENS = 90_000  # ... or when its last request sent more input tokens than this
IDLE_END_S = 60  # an autonomy session or goal worker with no turn for this long has finished
BRIDGE_QUIET_S = 2.0  # bridge an undelegated request after this much user silence
BRIDGE_AFTER_L_TURN_S = 3.0  # ... but not if an L turn started this recently (it has the request)
DEDUPE_S = 20.0  # the same line (role + words) isn't logged twice within this window
# Robot MCP tools that move the body: hidden from autonomy sessions when the body gates fail.
BODY_TOOLS = ["say", "run_skill", "play_frames", "set_pose", "set_torque", "trial", "compare", "body_sweep",
              "servo_calibrate", "calibration", "look", "scan", "video", "simulation_execute"]
# After a stop, autonomy keeps the body still for a while unless the owner asks for a move (Autonomy.on_stop): an
# utterance that names a move and has no negation / stillness word and isn't a question about the past.
MOVE_WORDS = re.compile(r"\b(walk|wave|turn (?:to the )?(?:left|right)|turn around|step|move|stand up|get up|sit"
                        r"|dance|kick|bow|squat|lie down|march|forward|backward|raise|lift)\b", re.I)
NOT_A_MOVE = re.compile(r"\b(don'?t|do not|doesn'?t|no|not|never|stop|stay|still|quiet|wait|freeze|halt|hold on"
                        r"|enough)\b|^\W*(?:hey )?(?:tony\W+)?(?:what|why|when|where|who|which|did|have|has|were|was"
                        r"|how (?:did|was|were|is|are|far|long|many|much))\b", re.I)
PROMPT_MODULES = os.environ.get("TONYPI_LIVE_PROMPT_MODULES", "m1_style,m2_language,m3_unclear,m4_pronounce,m5_reuse")
PROMPT_VARIANT = os.environ.get("TONYPI_LIVE_PROMPT", "voice")
AUTONOMY_PROMPT = (  # used only if prompts/codex_autonomy.md is missing
    "You are Tony's autonomy session: nobody is talking to Tony, so you work on the goal set on this thread. Learn by "
    "measured trials (trial/compare/body_sweep, guard_tilt_deg; falls are data: get_up and continue), never ask about "
    "setup, keep the ledger with the `tony` CLI (goal notes, journal, lessons), end in a stable pose, keep commits "
    "small and tested, never touch safety code. Say one short line with `tony say` only for notable results.")


def live_prompt() -> str:
    """GPT-Live's prompt; the only text kept verbatim through its context replacement. Variants (TONYPI_LIVE_PROMPT):
    voice (default) = prompts/live_voice.md, falling back to service.LIVE_PROMPT if it is missing; legacy =
    service.LIVE_PROMPT (the short relay prompt that delegated reliably in live tests); core = the spec's prompt
    (docs/LIVE_MODE_SPEC.md: its "Generate <|SpawnThinking|>" wording made GPT-Live answer by itself); plain = core with
    that wording replaced. core/plain add the live_m* modules."""
    if PROMPT_VARIANT == "legacy":
        return LIVE_PROMPT
    try:
        core = (PROMPTS / ("live_voice.md" if PROMPT_VARIANT == "voice" else "live_core.md")).read_text()
    except OSError as e:
        log.warning("voice prompt missing (%s): using the built-in one", e)
        return LIVE_PROMPT
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
    if PROMPT_VARIANT != "voice":
        for m in [x.strip() for x in PROMPT_MODULES.split(",") if x.strip()]:
            if (f := PROMPTS / f"live_{m}.md").exists():
                parts.append(f.read_text().strip())
    return "\n\n".join(parts)


def _text(name: str, fallback: str | None = None) -> str:
    try:
        return (PROMPTS / name).read_text().strip()
    except OSError:
        if fallback is None:
            raise
        log.warning("prompt %s missing: using the built-in fallback", name)
        return fallback


def _mark_preempt(t: float) -> None:
    from tonypi.brain import mark_preempt

    mark_preempt(t)


def _brain_store():
    from tonypi.brain import store

    return store


def _mem(fn: str, *a, **kw):
    """tonypi.brain.store.<fn>(...): memory, journal and goals must never break the voice, so errors are logged."""
    try:
        return getattr(_brain_store(), fn)(*a, **kw)
    except Exception as e:
        log.warning("brain store %s failed: %s", fn, e)
        return None


def _with_memory(text: str) -> str:
    snap = _mem("memory_snapshot")
    return f"{text}\n\n## Memory snapshot\n{snap}" if snap else text


def _append_jsonl(path: Path, row: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as f:
            f.write(json.dumps(row) + "\n")
    except OSError as e:
        log.warning("can't append to %s: %s", path, e)


def _fn(name: str, description: str, props: dict, required: list[str]) -> dict:
    return {"type": "function", "name": name, "description": description,
            "inputSchema": {"type": "object", "properties": props, "required": required, "additionalProperties": False}}


DYNAMIC_TOOLS = [
    _fn("tony_speak_now", "Make Tony say something aloud now, before your turn ends: a short progress update the user "
        "should hear during long work, or a warning. One or two short spoken sentences. Not for the final answer (that "
        "is your [FINAL] message).", {"text": {"type": "string"}}, ["text"]),
    _fn("tony_start_task", "Start a background Codex worker for independent work longer than ~20 s (code changes, "
        "tests, research, trial batches, gait tuning). Returns a task id at once; the result is spoken to the user "
        "once and put into this thread when it finishes (don't repeat it). The brief must be self-contained. With "
        "`goal` the worker keeps going (several turns) until the goal's criteria are met, it is blocked, or its budget "
        "runs out.",
        {"title": {"type": "string", "description": "3-6 words, spoken to the user"},
         "brief": {"type": "string", "description": "Self-contained: goal, the user's exact words, constraints, "
                   "decisions made, files, how to verify, what to report"},
         "uses_body": {"type": "boolean", "description": "true if the worker will move the robot or use the head "
                       "camera; then you must not move the body yourself until it finishes"},
         "goal": {"type": "string", "description": "measurable success criteria; makes the worker persist until met"},
         "goal_id": {"type": "string", "description": "the ledger goal this serves (e.g. g3), if any"},
         "tier": {"type": "string", "enum": ["terra", "sol"], "description": "model: terra (default) or sol for hard "
                  "reasoning/code"},
         "full_context": {"type": "boolean", "description": "fork this thread (whole conversation) instead of brief + "
                          "recent transcript; expensive"},
         "effort": {"type": "string", "enum": ["low", "medium", "high", "xhigh"]}},
        ["title", "brief", "uses_body"]),
    _fn("tony_tasks", "List background tasks (id, title, status, age, result if done) and the autonomy session.", {}, []),
    _fn("tony_steer_task", "Send a correction or new detail to a running background task.",
        {"task_id": {"type": "string"}, "text": {"type": "string"}}, ["task_id", "text"]),
    _fn("tony_cancel_task", "Interrupt a running background task (task_id='all' for every task). Returns the "
        "confirmed status.", {"task_id": {"type": "string"}}, ["task_id"]),
    _fn("tony_switch_brain", "Switch Tony to the offline local agent (LFM3 on the Pi; it stops this voice service). "
        "The voice service announces it and switches; reply only with a [COMMENTARY] line afterwards.",
        {"name": {"type": "string", "enum": ["local"]}}, ["name"]),
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
    "required": ["status", "spoken_sentence", "details", "lesson", "commits", "needs_voice_restart", "question"],
    "properties": {
        "status": {"type": "string", "enum": ["done", "partial", "failed", "blocked"]},
        "spoken_sentence": {"type": "string", "description": "one plain spoken sentence: outcome + key fact"},
        "details": {"type": "string", "description": "what changed (files, commits), how verified, what is left"},
        "lesson": {"type": ["string", "null"], "description": "one reusable lesson learned, or null"},
        "commits": {"type": "array", "items": {"type": "string"}},
        "needs_voice_restart": {"type": "boolean"},
        "question": {"type": ["string", "null"], "description": "only for a decision only the owner can make "
                     "(status blocked); never about setup, clearance or observers"},
    },
}


def parse_result(raw: str) -> dict | None:
    """A worker's final JSON (also when wrapped in prose or a code fence)."""
    for text in (raw, raw[raw.find("{"):raw.rfind("}") + 1]):
        try:
            res = json.loads(text)
        except ValueError:
            continue
        if isinstance(res, dict) and "status" in res:
            return res
    return None


class Conversation:
    """Append-only log of what was said (voice, typed, results): seeds GPT-Live after restarts, briefs workers, and is
    indexed by `tony recall`. The same line (role + words) within DEDUPE_S is logged once (bridge + delegation)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._seen: dict[tuple[str, str], float] = {}
        path.parent.mkdir(parents=True, exist_ok=True)

    def add(self, role: str, text: str, **kw) -> bool:
        text = (text or "").strip()
        if not text:
            return False
        now = time.time()
        key = (role, " ".join(re.sub(r"[^\w\s]", " ", text.lower()).split()))
        if now - self._seen.get(key, -1e9) < DEDUPE_S:
            return False
        self._seen = {k: t for k, t in self._seen.items() if now - t < DEDUPE_S}
        self._seen[key] = now
        with self.path.open("a") as f:
            f.write(json.dumps({"t": round(now, 1), "role": role, "text": text, **kw}) + "\n")
        if self.path.stat().st_size > 5_000_000:
            self.path.rename(self.path.with_suffix(f".{time.strftime('%Y%m%d-%H%M')}.jsonl"))
        return True

    def tail(self, n: int) -> list[dict]:
        if not self.path.exists():
            return []
        out = []
        for x in self.path.read_text().splitlines()[-n:]:
            try:
                out.append(json.loads(x))
            except ValueError:
                pass
        return out


# How a request that didn't come through GPT-Live reached the brain (voice.sock "text" op, by its source).
SOURCE_LABELS = {
    "phone_mic": "Source: heard by Tony's iPhone on its stand (companion mode; its speech recognition may contain "
                 "mistakes). Tony's own mic heard no speech around then, so this is not a copy of a voice request",
    "phone_app": "Source: typed in Tony's iPhone app (not spoken)",
}
# A companion phone and Tony's own mic often hear the same words: a phone_mic request is dropped if Tony's mic heard
# user speech from PHONE_ECHO_S before it arrived until PHONE_WAIT_S after (by then GPT-Live has its own copy).
PHONE_ECHO_S, PHONE_WAIT_S = 8.0, 2.5
# ... and dropped as Tony's own voice (the phone hears his speaker; his mic is muted while he talks) when it arrives
# while he talks or PHONE_SELF_TAIL_S after, or matches a line he said in the last PHONE_SELF_RECENT_S.
PHONE_SELF_TAIL_S, PHONE_SELF_RECENT_S = 3.0, 20.0


def _words(text: str) -> list[str]:
    """Lowercase words without the wake word, for echo matching."""
    return [w for w in re.findall(r"[a-z0-9']+", (text or "").lower()) if w not in ("tony", "hey", "toni")]


class LiveBrain(Live):
    def __init__(self, hub) -> None:
        super().__init__(hub)
        self.codex_home = CODEX_HOME_LIVE
        self._init_state()

    def _init_state(self) -> None:
        self.state_path = DATA / "state.json"
        self.convo = Conversation(DATA / "conversation.jsonl")
        self.l_turn: str | None = None  # L's active turn id
        self.workers: dict[str, dict] = {}  # task id -> {thread, turn, title, status, started, uses_body, result, ...}
        self.by_thread: dict[str, str] = {}  # worker thread id -> task id
        self.last_message: dict[str, str] = {}  # thread id -> last agentMessage text
        self.thread_tokens: dict[str, int] = {}  # thread id -> total tokens (thread/tokenUsage/updated)
        self.restart_after_turn: str | None = None
        # Codex's own link to the live call (its "sideband") sometimes misses a delegation, or its turn stalls; our data
        # channel sees every delegation, so we bridge: hand the request to L ourselves and speak L's answer.
        self.codex_took: float = 0.0  # last time Codex reported a handoff_request
        self.bridged: dict[str, str] = {}  # L turn id -> delegation item id we answer ourselves
        self.l_turn_started = self.l_last_item = 0.0
        self.last_delegation: tuple[str, str] | None = None
        self.last_delegation_t = 0.0
        self.last_user_speech_t = 0.0  # Tony's own mic: last user transcript piece
        self.last_input_t = time.monotonic()  # last request from anyone (a fresh start counts: no instant autonomy)
        self._l_tokens_saved = 0
        self._utt = {"user": [], "assistant": []}
        self._frags: list[str] = []  # utterances GPT-Live may not have handed off yet
        self._frag_t = 0.0
        self._frag_task: asyncio.Task | None = None
        self.autonomy = None  # tonypi.voice.autonomy.Autonomy, started by start_autonomy()
        self.auto: dict | None = None  # the autonomy session: {thread, turn, goal_id, title, status, last, t0, ...}
        self.auto_tokens = 0  # tokens used by autonomy threads since the service started

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

    def _next_task_id(self) -> str:
        """Unique across restarts (the counter lives in state.json): e.g. 1030-t7."""
        n = int(self._load_state().get("task_seq", 0)) + 1
        self._save_state(task_seq=n)
        return f"{time.strftime('%m%d')}-t{n}"

    def _l_settings(self) -> dict:
        return {"cwd": REPO, "model": MODEL, "sandbox": "danger-full-access", "approvalPolicy": "never",
                "developerInstructions": _with_memory(_text("codex_developer.md")),
                "serviceTier": SERVICE_TIER,
                "config": {"model_reasoning_effort": L_EFFORT, "model_reasoning_summary": "concise",
                           "service_tier": SERVICE_TIER}}

    @staticmethod
    def _rotate_reason(st: dict) -> str:
        """Why the saved L thread must not be resumed ('' = resume it)."""
        if st.get("tools_hash") != TOOLS_HASH:
            return "Tony's tools changed"
        created = st.get("created_t")
        if created is None and st.get("created"):
            try:
                created = time.mktime(time.strptime(st["created"], "%Y-%m-%dT%H:%M:%S"))
            except ValueError:
                created = 0
        if time.time() - (created or 0) > L_MAX_AGE_S:
            return "it was older than 20 hours"
        if int(st.get("l_input_tokens") or 0) > L_MAX_INPUT_TOKENS:
            return f"its context reached {int(st['l_input_tokens']) // 1000}k tokens"
        return ""

    async def setup_thread(self) -> str:
        st = self._load_state()
        old = st.get("thread")
        why = self._rotate_reason(st) if old else ""
        if old and not why:
            try:
                r = await self._rpc("thread/resume", {"threadId": old, "excludeTurns": True, **self._l_settings()},
                                    timeout=60)
                log.info("brain thread resumed: %s", old)
                return r["thread"]["id"]
            except Exception as e:
                why = f"resuming it failed ({e})"
                log.warning("resume of %s failed (%s): new brain thread", old, e)
        r = await self._rpc("thread/start", {**self._l_settings(), "ephemeral": False, "serviceName": "tonypi-live",
                                             "dynamicTools": DYNAMIC_TOOLS}, timeout=60)
        tid = r["thread"]["id"]
        if old:
            await self._inject(tid, f"Continuity note: you replace Tony's earlier brain thread {old} ({why}). Your "
                               f"memory lives in {BRAIN_DIR}: MEMORY.md (snapshot in your instructions), goals.json "
                               "(`tony goal list`), journal/ (`tony history --today` shows what just happened) and "
                               "`tony recall QUERY` searches past conversations, tasks and trials. The recent "
                               f"conversation is in {DATA / 'conversation.jsonl'}, background task results in "
                               f"{DATA / 'tasks.jsonl'}.")
            _mem("journal", "note", f"new brain thread {tid} ({why})")
        self._save_state(thread=tid, tools_hash=TOOLS_HASH, created=time.strftime("%Y-%m-%dT%H:%M:%S"),
                         created_t=round(time.time()), l_input_tokens=0, previous_thread=old)
        log.info("new brain thread: %s (%s)", tid, why or "first")
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
        if getattr(self, "wake_reason", "").startswith("wake word"):
            note.append("The user just woke you by saying your name; their words follow. If they said only your "
                        "name, answer with one short word such as \"Yes?\"; otherwise hand their request to the brain.")
        reason = self._load_state().get("restart_reason")
        if reason:
            note.append(f"The voice just restarted ({reason}). Greet the user in a few words, e.g. \"I'm back.\"")
            self._save_state(restart_reason=None)
        # Earlier turns go in as one quiet developer note, NOT as user/assistant items: seeded as real turns, GPT-Live
        # imitated Tony's old replies ("Okay, waving now.") and stopped handing requests to the brain (2026-10-30).
        turns = [t for t in self.convo.tail(40) if t.get("role") in ("user", "assistant")][-16:]
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
        if source == "phone_mic":
            arrived = time.monotonic()
            if why := self._phone_echo(text, arrived):
                log.info("phone_mic %r dropped: %s", text, why)
                return {"dropped": why}
            await asyncio.sleep(PHONE_WAIT_S)
            if self.last_user_speech_t >= arrived - PHONE_ECHO_S:
                log.info("phone_mic %r dropped: Tony's own mic heard the user too", text)
                return {"dropped": "Tony's own microphone heard the user at the same time; the voice has it"}
        else:
            self._move_asked(text)  # phone speech doesn't clear the stop latch, nor the body pause
        self._user_activity()
        self.convo.add("user", text, source=source)
        if hasattr(self.hub, "wake"):  # L's answer is spoken by the voice session: have one
            await self.hub.wake(f"{source} request", hold=False)
        inp = [{"type": "text", "text": text}]
        label = SOURCE_LABELS.get(source, f"Source: {source} in the app (not spoken)")
        ctx = {"source": {"kind": "application", "value": label}}
        if self.l_turn:
            try:
                await self._rpc("turn/steer", {"threadId": self.tid, "expectedTurnId": self.l_turn, "input": inp,
                                               "additionalContext": ctx})
                return {"steered": self.l_turn}
            except Exception as e:
                log.info("steer failed (%s): starting a turn", e)
        r = await self._rpc("turn/start", {"threadId": self.tid, "input": inp, "additionalContext": ctx})
        return {"turn": (r.get("turn") or {}).get("id")}

    def _phone_echo(self, text: str, arrived: float) -> str:
        """Why a phone_mic request is Tony's own voice coming back from his speaker ('' = it isn't). Tony's mic is
        muted while he talks, so the phone is the only one that hears him."""
        audio = self.audio
        talking_until = max(getattr(audio, "voice_until", 0.0) or 0.0, getattr(audio, "mute_mic_until", 0.0) or 0.0)
        if getattr(audio, "speaking_local", False) or arrived <= talking_until + PHONE_SELF_TAIL_S:
            return "it arrived while Tony was talking (or just after): his own voice"
        words = _words(text)
        if len(words) < 2:
            return ""
        now = time.time()
        recent = ["".join(self._utt.get("assistant") or [])]
        recent += [r.get("text", "") for r in self.convo.tail(30)
                   if r.get("role") == "assistant" and now - float(r.get("t") or 0) < PHONE_SELF_RECENT_S]
        for line in recent:
            said = _words(line)
            if not said:
                continue
            inside = sum(w in set(said) for w in words) / len(words)
            ratio = difflib.SequenceMatcher(None, " ".join(words), " ".join(said)).ratio()
            if inside >= 0.8 or ratio >= 0.6:
                return f"it matches what Tony just said ({line[:60]!r}): his own voice"
        return ""

    async def interrupt_all(self, body_only: bool = False) -> None:
        """The stop reflex: interrupt L's turn, body tasks and the autonomy session (tonyd was already stopped and
        latched); autonomy keeps the body still for a while (Autonomy.on_stop)."""
        if self.autonomy is not None:
            asyncio.create_task(self.autonomy.on_stop())
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

    def busy(self) -> bool:
        """The voice stays awake while L works, a background task runs (its result is spoken) or a spoken request
        may still be bridged. An autonomy session doesn't count: that is Tony's own work."""
        return (self.l_turn is not None or bool(self._frags) or self.restart_after_turn is not None
                or any(w["status"] == "running" for w in self.workers.values()))

    def last_activity(self) -> float:
        return max(super().last_activity(), self.last_user_speech_t, self.last_input_t, self.l_turn_started,
                   self.l_last_item, self.last_delegation_t)

    def speak(self, text: str) -> bool:
        """Spoken by GPT-Live while the session is up; while it sleeps, by Piper on the speaker."""
        if super().speak(text):
            return True
        say_local = getattr(self.hub, "say_local", None)
        if say_local is None or getattr(self.hub, "voice", "on") == "starting":
            return False
        asyncio.get_running_loop().create_task(say_local(text))
        return True

    def _user_activity(self) -> None:
        """The owner spoke or typed: an autonomy session gives him the robot and the brain back."""
        self.last_input_t = time.monotonic()
        # Only the scheduler's own session counts (it handles "starting" and repeats); self.auto may still be the
        # previous, closed session while a new one starts.
        if self.autonomy is not None and getattr(self.autonomy, "session", None):
            asyncio.create_task(self.autonomy.on_user_activity())

    def _move_asked(self, text: str) -> None:
        """After a stop, autonomy keeps the body still for 30 min, unless the owner asks for a move ("walk forward";
        not "don't move", "stand still" or "what did you learn")."""
        if not MOVE_WORDS.search(text or "") or NOT_A_MOVE.search(text or ""):
            return
        st = _mem("autonomy_state") or {}
        if st.get("body_paused_until", 0) > time.time():
            _mem("set_autonomy", body_paused_until=0)
            log.info("owner asked for a move: autonomy body pause cleared")

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
            self.last_user_speech_t = time.monotonic()
            if time.monotonic() > self.audio.voice_until + 0.3:  # not Tony's own voice coming back
                self._user_activity()
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
        if self.codex_took >= since or self.l_turn_started >= since or not text:
            return
        log.warning("Codex didn't take delegation %r: bridging it", text)
        await self._bridge(item_id, text)

    def utterance_done(self, text: str) -> None:
        """Called by the Hub when a user utterance pauses (1 s). GPT-Live sometimes answers without handing off
        ("Okay, marching" and nothing moves), and people pause mid-sentence: fragments are collected and bridged as
        one request once the user was quiet BRIDGE_QUIET_S and nothing reached the brain since the first one."""
        text = " ".join((text or "").split())
        if not text:
            return
        self._move_asked(text)
        if not self._frags:
            self._frag_t = time.monotonic() - 1.5  # the delegation may come just before the pause
        self._frags.append(text)
        if self._frag_task is None or self._frag_task.done():
            self._frag_task = asyncio.create_task(self._bridge_fragments())

    async def _bridge_fragments(self) -> None:
        while self._frags:
            await asyncio.sleep(0.25)
            now = time.monotonic()
            if self.last_delegation_t >= self._frag_t or self.l_turn_started >= self._frag_t:
                self._frags = []  # GPT-Live (or Codex) handed it off
                return
            if now - self.last_user_speech_t < BRIDGE_QUIET_S:
                continue  # still talking
            text, self._frags = " ".join(self._frags), []
            if now - self.l_turn_started < BRIDGE_AFTER_L_TURN_S:
                return
            if len(re.findall(r"\w+", text)) <= 2:
                log.info("not bridging fragment %r", text)
                return
            log.warning("GPT-Live didn't hand off %r: bridging it", text)
            self.convo.add("user", text, source="voice (bridged)")
            await self._bridge("", text)

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
        try:
            r = await self._rpc("turn/start", {"threadId": self.tid, "input": inp})
        except Exception as e:
            log.warning("bridge turn/start failed for %r: %s", text, e)
            return
        turn = (r.get("turn") or {}).get("id")
        if turn:
            self.bridged[turn] = item_id

    async def watchdog(self) -> None:
        """A turn with no progress for 30 s is stuck (seen when Codex's link to the call dropped): interrupt, re-run.
        Also reports goal workers that went idle without finishing their goal."""
        while not self.closed.is_set():
            await asyncio.sleep(5)
            try:
                await self._watch_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # one failed RPC must not end the watchdog (it also reports idle goal workers)
                log.exception("watchdog")

    async def _watch_once(self) -> None:
        if self.l_turn and time.monotonic() - max(self.l_turn_started, self.l_last_item) > 30:
            stuck, self.l_turn = self.l_turn, None
            log.warning("brain turn %s stuck for 30 s: interrupting and re-running", stuck)
            try:
                await self._rpc("turn/interrupt", {"threadId": self.tid, "turnId": stuck}, timeout=10)
            except Exception as e:
                log.warning("interrupt failed: %s", e)
            if self.last_delegation:
                try:
                    await self._bridge(*self.last_delegation)
                except Exception as e:
                    log.warning("re-running the stuck request failed: %s", e)
        for task_id, w in list(self.workers.items()):
            if (w["status"] == "running" and w.get("goal") and w.get("turn") is None and not w.get("reporting")
                    and time.monotonic() - w.get("idle_t", time.monotonic()) > IDLE_END_S):
                try:
                    await self._report(task_id)
                except Exception:
                    log.exception("reporting idle worker %s", task_id)

    def on_notification(self, method: str, params: dict) -> None:
        tid = params.get("threadId")
        auto = self.auto if self.auto and tid == self.auto.get("thread") else None
        if method.startswith("item/"):
            if tid == self.tid:
                self.l_last_item = time.monotonic()
            elif auto:
                auto["last"] = time.monotonic()
        if method == "thread/realtime/itemAdded" and (params.get("item") or {}).get("type") == "handoff_request":
            self.codex_took = time.monotonic()
        if method.startswith(("turn/", "item/started", "item/completed", "thread/realtime/item", "thread/goal/")) \
                or "error" in method:
            it = params.get("item") or {}
            who = "L" if tid == self.tid else "auto" if auto else self.by_thread.get(tid, tid)
            log.info("codex %s %s %s", method, who, it.get("type", "") or json.dumps(params)[:160])
        if method == "thread/tokenUsage/updated":
            self._on_tokens(tid, params.get("tokenUsage") or {})
        elif method == "thread/goal/updated":
            status = (params.get("goal") or {}).get("status")
            if auto:
                auto["status"] = status
            elif tid in self.by_thread:
                w = self.workers[self.by_thread[tid]]
                w["goal_status"] = status
                if (status != "active" and w["status"] == "running" and w.get("turn") is None
                        and not w.get("reporting")):
                    asyncio.create_task(self._report(self.by_thread[tid]))
        elif method == "turn/started":
            turn = (params.get("turn") or {}).get("id")
            if tid == self.tid:
                self.l_turn = turn
                self.l_turn_started = time.monotonic()
            elif auto:
                auto["turn"], auto["last"] = turn, time.monotonic()
            elif tid in self.by_thread:
                w = self.workers[self.by_thread[tid]]
                w["turn"] = turn
                w.pop("idle_t", None)
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
            elif auto:
                auto["turn"], auto["last"] = None, time.monotonic()
            elif tid in self.by_thread:
                asyncio.create_task(self._worker_turn_done(self.by_thread[tid], params.get("turn") or {}))

    def _on_tokens(self, tid: str | None, usage: dict) -> None:
        total = int((usage.get("total") or {}).get("totalTokens") or 0)
        prev = self.thread_tokens.get(tid or "", 0)
        self.thread_tokens[tid or ""] = total
        if self.auto and tid == self.auto.get("thread") and total > prev:
            self.auto_tokens += total - prev
            self.auto["tokens"] = self.auto.get("tokens", 0) + total - prev
        if tid == self.tid:  # L's context size, for rotation at the next start
            last_in = int((usage.get("last") or {}).get("inputTokens") or 0)
            if abs(last_in - self._l_tokens_saved) >= 5000:
                self._l_tokens_saved = last_in
                self._save_state(l_input_tokens=last_in)

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
            now = time.monotonic()
            lines = [f"{k}: {w['title']} ({w['status']}, {int(now - w['started'])} s)"
                     + (f" -> {w['result'].get('spoken_sentence', '')}" if w.get("result") else "")
                     for k, w in self.workers.items()]
            if self.auto and not self.auto.get("closed"):
                lines.append(f"autonomy session: {self.auto['goal_id']} '{self.auto['title']}' "
                             f"({int(time.time() - self.auto['t0'])} s; ends when the owner speaks)")
            return "\n".join(lines) or "no background tasks"
        if name == "tony_steer_task":
            w = self.workers.get(a.get("task_id", ""))
            if not w or w["status"] != "running":
                return "no such running task"
            inp = [{"type": "text", "text": f"[steer] {a.get('text', '')}"}]
            if w.get("turn"):
                await self._rpc("turn/steer", {"threadId": w["thread"], "expectedTurnId": w["turn"], "input": inp})
            else:  # a goal worker between turns
                await self._rpc("turn/start", {"threadId": w["thread"], "input": inp})
            return "steered"
        if name == "tony_cancel_task":
            ids = [k for k, w in self.workers.items() if w["status"] == "running"] if a.get("task_id") == "all" \
                else [a.get("task_id", "")]
            return "; ".join([await self._cancel(k) for k in ids]) or "nothing running"
        if name == "tony_switch_brain":
            self.hub.request_brain_switch("local")
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

    # ---- background workers -----------------------------------------------------------------------------

    async def _start_task(self, a: dict) -> str:
        running = [w for w in self.workers.values() if w["status"] == "running"]
        if len(running) >= MAX_WORKERS:
            return f"refused: {MAX_WORKERS} tasks already running ({', '.join(w['title'] for w in running)})"
        uses_body = bool(a.get("uses_body"))
        if uses_body and any(w["uses_body"] for w in running):
            return "refused: another background task already uses the body; cancel it first"
        task_id = self._next_task_id()
        effort = a.get("effort") or "low"
        tier = a.get("tier") if a.get("tier") in MODELS else "terra"
        goal = str(a.get("goal") or "").strip()
        cfg = {"model_reasoning_effort": effort, "model_reasoning_summary": "concise",
               "features.multi_agent": False, "service_tier": SERVICE_TIER}
        common = {"model": MODELS[tier], "serviceTier": SERVICE_TIER, "sandbox": "danger-full-access",
                  "approvalPolicy": "never", "ephemeral": False,  # persistent: rollouts stay, native goals work
                  "developerInstructions": _text("codex_worker.md"), "config": cfg}
        if a.get("full_context"):
            r = await self._rpc("thread/fork", {"threadId": self.tid, "excludeTurns": True, **common}, timeout=60)
        else:
            r = await self._rpc("thread/start", {"cwd": REPO, "serviceName": "tonypi-task", **common}, timeout=60)
        thread = r["thread"]["id"]
        source = "owner" if time.monotonic() - max(self.last_user_speech_t, self.last_input_t) < 180 else "self"
        self.workers[task_id] = {"thread": thread, "turn": None, "title": a.get("title", ""),
                                 "brief": a.get("brief", ""), "status": "running", "started": time.monotonic(),
                                 "t_start": round(time.time()), "uses_body": uses_body, "goal": goal,
                                 "goal_id": a.get("goal_id"), "source": source, "tier": tier, "effort": effort}
        self.by_thread[thread] = task_id
        if goal:
            await self._rpc("thread/goal/set", {"threadId": thread, "tokenBudget": WORKER_GOAL_TOKENS,
                                                "objective": f"Task {task_id} {a.get('title', '')}: {goal}"[:3900]})
        convo = "\n".join(f"{t['role']}: {t['text'][:300]}" for t in self.convo.tail(30))[-3000:]
        ctx = {"task": {"kind": "application",  # only untrusted entries may carry a name
                        "value": f"Task metadata: id {task_id}; uses_body={uses_body}; requested "
                                 f"{time.strftime('%H:%M')} by Tony's main brain"
                                 + (f"; ledger goal {a['goal_id']}" if a.get("goal_id") else "")},
               "conversation": {"kind": "untrusted", "name": "recent_conversation_asr_transcript",
                                "value": convo or "(none)"}}
        if snap := _mem("memory_snapshot"):
            ctx["memory"] = {"kind": "application", "value": f"Tony's memory snapshot ({BRAIN_DIR}):\n{snap}"}
        turn = {"threadId": thread, "effort": effort, "additionalContext": ctx,
                "input": [{"type": "text", "text": f"BACKGROUND TASK {task_id}: {a.get('title', '')}\n{a.get('brief', '')}"}]}
        if not goal:  # a goal worker gets its schema in a final report turn (goal continuations can't carry one)
            turn["outputSchema"] = WORKER_SCHEMA
        t = await self._rpc("turn/start", turn, timeout=60)
        w = self.workers[task_id]
        if w["turn"] is None and "idle_t" not in w and not w.get("done"):  # unless turn/started already came
            w["turn"] = (t.get("turn") or {}).get("id")
        log.info("task %s started (%s %s, body=%s, goal=%s): %s", task_id, tier, effort, uses_body, bool(goal),
                 a.get("title"))
        return f"started {task_id}; its result will be spoken and put into this thread when done"

    async def _cancel(self, task_id: str) -> str:
        w = self.workers.get(task_id)
        if not w or w["status"] != "running":
            return f"{task_id}: not running"
        if w.get("goal"):
            try:
                await self._rpc("thread/goal/set", {"threadId": w["thread"], "status": "paused"}, timeout=10)
            except Exception as e:
                log.warning("pausing %s's goal failed: %s", task_id, e)
        if w.get("turn"):
            try:
                await self._rpc("turn/interrupt", {"threadId": w["thread"], "turnId": w["turn"]}, timeout=10)
            except Exception as e:
                return f"{task_id}: interrupt failed ({e})"
            w["status"] = "interrupted"  # its turn/completed reports it
        else:
            w["status"] = "interrupted"
            asyncio.create_task(self._worker_done(task_id, {"status": "interrupted"}))
        return f"{task_id}: interrupted"

    async def _worker_turn_done(self, task_id: str, turn: dict) -> None:
        w = self.workers[task_id]
        if (w["status"] == "running" and w.get("goal") and not w.get("reporting")
                and turn.get("status") in (None, "completed")):
            w["turn"], w["idle_t"] = None, time.monotonic()
            if w.get("goal_status") not in (None, "active"):
                await self._report(task_id)
            return  # Codex continues an active goal by itself; the watchdog reports it if it goes idle
        await self._worker_done(task_id, turn)

    async def _report(self, task_id: str) -> None:
        """A goal worker's goal ended (or it went idle): one last turn for the structured result."""
        w = self.workers[task_id]
        if w.get("reporting") or w["status"] != "running":
            return
        w["reporting"] = True
        status = w.get("goal_status") or "idle"
        try:
            if status in ("active", "idle"):  # no more continuation turns after the report
                await self._rpc("thread/goal/set", {"threadId": w["thread"], "status": "paused"}, timeout=10)
            t = await self._rpc("turn/start", {
                "threadId": w["thread"], "effort": "low", "outputSchema": WORKER_SCHEMA,
                "input": [{"type": "text", "text": f"Your goal for task {task_id} ended ({status}). Report now: "
                           "reply only with the final JSON result."}]}, timeout=60)
            if w.get("turn") is None and not w.get("done"):
                w["turn"] = (t.get("turn") or {}).get("id")
        except Exception as e:
            log.warning("report turn for %s failed: %s", task_id, e)
            await self._worker_done(task_id, {"status": "completed"})

    async def _worker_done(self, task_id: str, turn: dict) -> None:
        w = self.workers[task_id]
        if w.get("done"):
            return
        w["done"] = True
        if w["status"] == "running":
            w["status"] = "completed" if turn.get("status") in (None, "completed") else str(turn.get("status"))
        raw = self.last_message.get(w["thread"], "")
        res = parse_result(raw) or {
            "status": "partial" if w["status"] == "completed" else "failed",
            "spoken_sentence": f"Background task {w['title']} " + (
                "finished." if w["status"] == "completed" else "was stopped."),
            "details": raw[:1500], "lesson": None, "commits": [], "needs_voice_restart": False, "question": None}
        w["result"] = res
        sentence = res.get("spoken_sentence") or f"{w['title']} is done."
        if res.get("status") == "blocked" and res.get("question"):
            sentence += " " + res["question"]
        spoken = w["status"] != "interrupted" and self.speak(sentence)  # a stopped task isn't announced
        log.info("task %s %s (%s): %s", task_id, w["status"], res.get("status"), sentence)
        self.convo.add("assistant", sentence, source=f"task {task_id}")
        lesson = (res.get("lesson") or "").strip() if isinstance(res.get("lesson"), str) else ""
        _append_jsonl(DATA / "tasks.jsonl", {
            "id": task_id, "source": w.get("source", "owner"), "goal_id": w.get("goal_id"), "title": w["title"],
            "brief": str(w.get("brief", ""))[:2000], "start": w.get("t_start"), "end": round(time.time()),
            "status": res.get("status"), "state": w["status"], "details": str(res.get("details", ""))[:3000],
            "lesson": lesson or None, "commits": res.get("commits") or [], "tokens": self.thread_tokens.get(w["thread"])})
        _mem("journal", "task", f"{task_id} {res.get('status')}: {w['title']}: {sentence}")
        if lesson:
            _mem("remember", lesson, kind="lesson")
        await self._inject(self.tid, f"Background task {task_id} '{w['title']}' finished ({res.get('status')}): "
                           f"{sentence} Details: {str(res.get('details', ''))[:1500]} Lesson: {lesson or '-'} "
                           f"Commits: {res.get('commits')} needs_voice_restart: {res.get('needs_voice_restart')}. "
                           + ("The voice service already spoke that sentence to the user: do not repeat it; mention "
                              "it only if asked." if spoken else "Nothing was spoken about it."))

    # ---- autonomy host (tonypi.voice.autonomy.Autonomy calls these) -------------------------------------

    def start_autonomy(self) -> asyncio.Task | None:
        """Start the autonomy scheduler next to the watchdog (TONYPI_AUTONOMY=0 turns it off)."""
        if os.environ.get("TONYPI_AUTONOMY", "1") == "0":
            return None
        try:
            from tonypi.voice.autonomy import Autonomy

            self.autonomy = Autonomy(self)
        except Exception as e:
            log.warning("autonomy unavailable: %s", e)
            return None
        return asyncio.create_task(self.autonomy.run())

    async def shutdown(self) -> None:
        """Before the app-server goes away (GPT-Live closed, voice restart): end a running autonomy session."""
        if self.autonomy is not None and getattr(self.autonomy, "session", None):
            try:
                await asyncio.wait_for(self.autonomy.end("voice restart"), 15)
            except Exception as e:
                log.warning("ending the autonomy session: %s", e)

    def quiet_s(self) -> float:
        return time.monotonic() - max(self.last_user_speech_t, self.last_input_t, self.l_turn_started,
                                      self.l_last_item)

    def l_busy(self) -> bool:
        return self.l_turn is not None

    def body_task_running(self) -> bool:
        return any(w["status"] == "running" and w["uses_body"] for w in self.workers.values())

    def session_running(self) -> bool:
        """False once the session is over (its goal ended, or no turn for IDLE_END_S); the scheduler then calls
        end_session, which cleans up (goal paused if still active, turn interrupted, body stopped) and logs it."""
        a = self.auto
        if not a or a.get("closed") or a.get("over"):
            return False
        if a.get("status") not in (None, "active"):
            a["over"] = f"goal {a['status']}"
            return False
        if a.get("turn") is None and time.monotonic() - a["last"] > IDLE_END_S:
            a["over"] = "idle"
            return False
        return True

    async def start_session(self, goal: dict, objective: str, body_ok: bool, budget_tokens: int) -> str:
        cfg = {"model_reasoning_effort": AUTONOMY_EFFORT, "model_reasoning_summary": "concise",
               "service_tier": SERVICE_TIER}
        if not body_ok:  # the body gates failed: no motion tools at all this session
            cfg["mcp_servers.tonypi.disabled_tools"] = BODY_TOOLS
        r = await self._rpc("thread/start", {
            "cwd": REPO, "model": MODEL, "sandbox": "danger-full-access", "approvalPolicy": "never",
            "ephemeral": False, "serviceName": "tonypi-autonomy", "serviceTier": SERVICE_TIER,
            "developerInstructions": _with_memory(_text("codex_autonomy.md", AUTONOMY_PROMPT)), "config": cfg},
            timeout=60)
        thread = r["thread"]["id"]
        self.auto = {"thread": thread, "turn": None, "goal_id": goal.get("id"), "title": goal.get("title", ""),
                     "status": None, "last": time.monotonic(), "t0": time.time(), "tokens": 0, "body_ok": body_ok}
        await self._rpc("thread/goal/set", {"threadId": thread, "objective": objective, "tokenBudget": budget_tokens})
        ctx = {"session": {"kind": "application", "value": (
            f"Autonomy session on goal {goal.get('id')}, started {time.strftime('%a %H:%M')}. Body "
            f"{'available' if body_ok else 'NOT available (motion tools are off)'}. When the owner talks to Tony this "
            "session is paused and the body stopped; nothing is lost if the ledger is up to date.")}}
        try:
            t = await self._rpc("turn/start", {"threadId": thread, "effort": AUTONOMY_EFFORT, "additionalContext": ctx,
                                               "input": [{"type": "text", "text": "Begin. Work toward the goal; keep "
                                                          "the ledger updated."}]}, timeout=60)
            self.auto["turn"] = self.auto["turn"] or (t.get("turn") or {}).get("id")
        except Exception as e:  # Codex may already have started a goal turn by itself
            log.warning("autonomy turn/start: %s", e)
        log.info("autonomy session %s on %s (body=%s, budget %dk)", thread, goal.get("id"), body_ok,
                 budget_tokens // 1000)
        return thread

    async def end_session(self, reason: str) -> None:
        """Every way a session ends comes through here. The body first, at once: the preempt mark (a learning batch
        of this session stops before its next move and skips its closing settle / get_up) and a tonyd stop (no
        latch), unless L already has a turn (the owner is being served: L's moves preempt the session's, and a stop
        now could cut one off). In parallel: pause the goal if it is still active (else Codex continues it on an
        untracked thread) and interrupt the running turn."""
        a = self.auto
        if not a or a.get("closed") or a.get("ending"):
            return
        a["ending"] = True
        stop = None
        if a.get("body_ok"):
            owner_t = time.time() - (time.monotonic() - self.l_turn_started) if self.l_busy() else time.time()
            try:
                _mark_preempt(owner_t)
            except Exception as e:
                log.warning("autonomy preempt mark: %s", e)
            if not self.l_busy():
                stop = asyncio.create_task(asyncio.to_thread(self._stop_body))
        calls = []
        if a.get("status") in (None, "active"):
            calls.append(self._rpc("thread/goal/set", {"threadId": a["thread"], "status": "paused"}, timeout=5))
        if a.get("turn"):
            calls.append(self._rpc("turn/interrupt", {"threadId": a["thread"], "turnId": a["turn"]}, timeout=5))
        for r in await asyncio.gather(*calls, return_exceptions=True):
            if isinstance(r, BaseException):
                log.warning("autonomy end (%s): %s", reason, r)
        if stop is not None:
            await stop
        self._auto_closed(a.get("over") or reason)

    @staticmethod
    def _stop_body() -> None:
        """tonyd stop without the latch, whether or not a job runs right now (a learning batch is idle between its
        moves; the preempt mark stops it there)."""
        try:
            from tonypi.tonyd.client import TonydClient

            c = TonydClient()
            try:
                c.stop(latch=False)
            finally:
                c.close()
        except Exception as e:
            log.warning("autonomy body stop: %s", e)

    def _auto_closed(self, reason: str) -> None:
        a = self.auto
        if not a or a.get("closed"):
            return
        a["closed"] = True
        last = self.last_message.get(a["thread"], "")
        _append_jsonl(DATA / "tasks.jsonl", {
            "id": f"auto-{a['thread'][:8]}", "source": "autonomy", "goal_id": a["goal_id"], "title": a["title"],
            "brief": "autonomy session", "start": round(a["t0"]), "end": round(time.time()), "status": reason,
            "details": last[:3000], "tokens": a.get("tokens", 0)})
        log.info("autonomy session on %s ended: %s", a["goal_id"], reason)
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        if self.tid:
            loop.create_task(self._inject(
                self.tid, f"Your autonomy session on {a['goal_id']} '{a['title']}' ended ({reason}). Its last "
                f"message: {last[:600] or '-'} (details: `tony goal list`, `tony history --today`)."))

    def tokens_used_today(self) -> int:
        return self.auto_tokens

    async def rate_limits(self) -> dict | None:
        return await self._rpc("account/rateLimits/read", None, timeout=10)

    def tonyd_state(self) -> dict | None:
        try:
            from tonypi.tonyd.client import TonydClient

            c = TonydClient()
            try:
                return c.state()
            finally:
                c.close()
        except Exception:
            return None
