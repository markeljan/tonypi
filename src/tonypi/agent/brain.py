"""Turns a request into a plan: what to say + which skill-level actions to run.

The local LLM (llama-server, OpenAI-compatible) answers with JSON constrained by a schema whose action names are an
enum, so it can only pick from ACTIONS. A few deterministic rules run around it: `stop` never reaches the LLM,
vision questions always use the camera, and a move named outright in the request is never dropped.
"""

from __future__ import annotations

import base64
import json
import os
import re
import urllib.request
from dataclasses import dataclass, field
from typing import Callable

# name -> description shown to the LLM. Implementations live in body.py.
ACTIONS: dict[str, str] = {
    "wave": "wave hello",
    "bow": "bow politely",
    "dance": "do a dance routine (about a minute)",
    "kung_fu": "kung fu moves",
    "twist": "twist the hips",
    "punch_left": "left uppercut",
    "punch_right": "right uppercut",
    "kick_left": "kick with the left foot",
    "kick_right": "kick with the right foot",
    "squat": "squat down and back up",
    "arms_up": "raise both arms",
    "flap_wings": "flap arms like wings",
    "step_in_place": "march in place",
    "walk_forward": "walk forward (count = steps)",
    "walk_backward": "walk backward (count = steps)",
    "turn_left": "turn left (count = steps)",
    "turn_right": "turn right (count = steps)",
    "step_left": "side-step left",
    "step_right": "side-step right",
    "look_left": "turn head left",
    "look_right": "turn head right",
    "look_up": "tilt head up",
    "look_down": "tilt head down",
    "look_center": "head straight",
    "nod": "nod yes",
    "shake_head": "shake head no",
    "describe_view": "look through the camera and describe what you see",
    "report_status": "tell your battery level, posture and joint temperatures",
    "lie_down": "lie down",
    "sleep": "go to sleep: lie down and switch the motors off (you still hear 'hey Tony')",
    "get_up": "get up from lying, or wake up from sleep",
    "stand": "stand up straight",
    "stop": "stop moving",
}

SCHEMA = {
    "type": "object",
    "properties": {
        "actions": {
            "type": "array",
            "maxItems": 6,
            "items": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": list(ACTIONS)},
                    "count": {"type": "integer", "minimum": 1, "maximum": 5},
                },
                "required": ["action", "count"],
            },
        },
        "say": {"type": "string"},
    },
    "required": ["actions", "say"],
}

SYSTEM = (
    "You are Tony, a small friendly humanoid robot (TonyPi). People talk to you; you reply out loud and can move.\n"
    'Answer with JSON: "actions" is the list of moves to do in order (empty if you only talk), '
    '"say" is what you speak (one or two short sentences, plain words, no emojis; may be empty). '
    "Only use these actions:\n"
    + "\n".join(f"- {k}: {v}" for k, v in ACTIONS.items())
    + "\nDo exactly what was asked: no extra moves. If asked something you can't do, say so briefly. "
    "Never say 'Hey Tony' yourself.\n"
    "Examples:\n"
    'dance -> {"actions":[{"action":"dance","count":1}],"say":"Let\'s dance!"}\n'
    'wave at us -> {"actions":[{"action":"wave","count":1}],"say":"Hello everyone!"}\n'
    'what is the capital of France -> {"actions":[],"say":"Paris."}\n'
    'what do you see -> {"actions":[{"action":"describe_view","count":1}],"say":"Let me take a look."}\n'
    'walk two steps and turn right -> {"actions":[{"action":"walk_forward","count":2},'
    '{"action":"turn_right","count":1}],"say":"On my way."}\n'
    'look around -> {"actions":[{"action":"look_left","count":1},{"action":"look_right","count":1},'
    '{"action":"look_center","count":1}],"say":""}\n'
    'do you agree -> {"actions":[{"action":"nod","count":1}],"say":"Yes, I do."}\n'
    'how is your battery -> {"actions":[{"action":"report_status","count":1}],"say":""}\n'
    'go to sleep -> {"actions":[{"action":"sleep","count":1}],"say":"Good night!"}\n'
    'wake up -> {"actions":[{"action":"get_up","count":1}],"say":"Good morning!"}'
)

# "enable debug mode" / "debug off" ... (checked before STOP_RE: "stop debug mode" must not stop the robot)
_DEBUG = r"de-?\s?bug(ging)?"
DEBUG_ON_RE = re.compile(rf"\b(enable|turn on|switch on|start|activate|enter)\b.{{0,20}}\b{_DEBUG}|\b{_DEBUG}( mode)? (on|enabled?)\b", re.I)
DEBUG_OFF_RE = re.compile(rf"\b(disable|turn off|switch off|stop|deactivate|exit|leave|end)\b.{{0,20}}\b{_DEBUG}|\b{_DEBUG}( mode)? (off|disabled?)\b", re.I)


def debug_command(text: str) -> bool | None:
    """True/False to switch debug mode on/off, None if the request isn't about debug mode."""
    if DEBUG_OFF_RE.search(text):
        return False
    if DEBUG_ON_RE.search(text):
        return True
    return None


STOP_RE = re.compile(r"^\W*(stop|halt|freeze|stay|enough|cancel|quiet|be quiet|shut up)\b", re.I)
VISION_RE = re.compile(r"\b(what (do|can) you see|what'?s in front|describe (what|the)|look at (this|that|me)|"
                       r"who'?s (here|there)|what is (this|that))\b", re.I)
STATUS_RE = re.compile(r"\b(battery|charge|temperature|how hot|too hot|overheat\w*|your status|"
                       r"how are you (feeling|doing))\b", re.I)
# a move named outright is always done, even if the LLM forgets it
KEYWORDS = [
    (re.compile(r"\bdanc(e|ed|es|ing)\b", re.I), "dance"),
    (re.compile(r"\bwav(e|ing)\b", re.I), "wave"),
    (re.compile(r"\bbow\b", re.I), "bow"),
    (re.compile(r"\b[kc][aou](ng|n|me)? ?f(u|ood|oo)\b|\bkarate\b", re.I), "kung_fu"),  # ASR: "confoo", "come foo"
    (re.compile(r"\bsquat\b", re.I), "squat"),
    (re.compile(r"\bnodd?(ing)?\b", re.I), "nod"),  # ASR: "nodd your head"
    (re.compile(r"\bshake your head\b", re.I), "shake_head"),
    (re.compile(r"\btwist\b", re.I), "twist"),
    (re.compile(r"\b(go to |time to )?sleep\b|\bpower (down|off)\b|\bshut down\b|\bnap\b", re.I), "sleep"),
    (re.compile(r"\blie down\b", re.I), "lie_down"),
    (re.compile(r"\bget up\b|\bstand up\b|\bwake up\b", re.I), "get_up"),
]


@dataclass
class Plan:
    actions: list[tuple[str, int]] = field(default_factory=list)
    say: str = ""
    source: str = "llm"
    early: int = 0  # how many of `actions` were already handed out while streaming


def _clean_say(text: str) -> str:
    return re.sub(r"[*_#`]", "", text or "").strip()


def postprocess(request: str, plan: Plan) -> Plan:
    acts: list[tuple[str, int]] = []
    for name, count in plan.actions:
        if name not in ACTIONS:
            continue
        count = max(1, min(5, int(count)))
        if acts and acts[-1][0] == name:  # small models like to repeat the same move
            continue
        acts.append((name, count))
    names = {a for a, _ in acts}
    if VISION_RE.search(request) and "describe_view" not in names:
        acts = [a for a in acts if not a[0].startswith("look_")] + [("describe_view", 1)]
    if STATUS_RE.search(request) and "report_status" not in names:
        acts.append(("report_status", 1))
        names.add("report_status")
    say = "" if "report_status" in names else plan.say  # the model can't know; real numbers are spoken instead
    if "describe_view" in names:
        say = "Let me take a look."  # the text model can't see; it tends to invent what's there
    for rx, name in KEYWORDS:
        if rx.search(request) and name not in names:
            acts.append((name, 1))
            names.add(name)
    if acts and acts[-1][0] == "stop":
        acts.pop()
    return Plan(acts[:6], _clean_say(say), plan.source)


AUDIO_NOTE = ("\nRequests may arrive as audio. The recording can start with the wake phrase 'Hey Tony' (sometimes after "
              "unrelated talk): act on what is said after it. Never say 'Hey Tony' yourself.")
# only follow-ups get the previous turn: history after the cached system prompt costs ~1.7 s per command
FOLLOWUP_RE = re.compile(r"\b(again|another|more|same|repeat|that|it|too|also|other|instead|last|previous|before)\b", re.I)
_ACTION_RE = re.compile(r'\{\s*"action"\s*:\s*"(\w+)"\s*,\s*"count"\s*:\s*(\d+)\s*\}')


class Brain:
    def __init__(self, url: str = os.environ.get("TONYPI_BRAIN_URL", "http://127.0.0.1:8080/v1/chat/completions")) -> None:
        self.url = url
        self.last: list[dict] = []  # previous turn, compact

    def plan(self, request: str, audio_wav: bytes | None = None) -> Plan:
        return self.plan_stream(request, audio_wav)

    def plan_stream(self, request: str, audio_wav: bytes | None = None,
                    on_action: Callable[[str, int], None] | None = None, cancel=None) -> Plan:
        """Plan for a typed request, or for spoken audio (`request` is then the rough streaming transcript, used
        for the rules). on_action(name, count) fires as soon as each move is generated (~0.9 s before the end);
        the returned plan holds every action, including those already handed out. cancel: a threading.Event that
        aborts the request (frees llama-server for the next one)."""
        request = request.strip()
        if STOP_RE.match(request):
            return Plan([("stop", 1)], "", "rule")
        early: list[tuple[str, int]] = []

        def early_action(name: str, count: int) -> None:
            # only moves postprocess() would keep as-is: valid, not a repeat, not a look_* that vision replaces
            count = max(1, min(5, count))
            if (name not in ACTIONS or name == "stop" or (early and early[-1][0] == name)
                    or (name.startswith("look_") and VISION_RE.search(request))):
                return
            early.append((name, count))
            if on_action:
                on_action(name, count)

        try:
            raw = self._llm(request, audio_wav, early_action if on_action else None, cancel)
            data = json.loads(raw)
            plan = Plan([(a["action"], a.get("count", 1)) for a in data.get("actions", [])], data.get("say", ""),
                        "llm+audio" if audio_wav else "llm")
            self.last = [{"role": "user", "content": request},
                         {"role": "assistant", "content": f"(did: {', '.join(a for a, _ in plan.actions) or 'nothing'};"
                                                          f" said: {plan.say!r})"}]
        except Exception as e:  # LLM down or bad JSON: rules only
            plan = Plan([], "", f"fallback ({type(e).__name__})")
        plan = postprocess(request, plan)
        for a in early:  # actions already started stay first, in order
            if a in plan.actions:
                plan.actions.remove(a)
        plan.actions = early + plan.actions
        plan.early = len(early)
        if not plan.actions and not plan.say:
            plan.say = "Sorry, I didn't understand that."
        return plan

    def _llm(self, request: str, audio_wav: bytes | None, on_action, cancel=None) -> str:
        if audio_wav:
            system, user = SYSTEM + AUDIO_NOTE, [{"type": "input_audio", "input_audio": {
                "data": base64.b64encode(audio_wav).decode(), "format": "wav"}}]
        else:
            system, user = SYSTEM, request
        history = self.last if FOLLOWUP_RE.search(request) else []
        body = {
            "messages": [{"role": "system", "content": system}, *history, {"role": "user", "content": user}],
            "max_tokens": 200,
            "temperature": 0.1,
            "cache_prompt": True,
            "stream": on_action is not None,
            "response_format": {"type": "json_schema", "json_schema": {"name": "plan", "schema": SCHEMA}},
            "chat_template_kwargs": {"enable_thinking": False},  # LFM3 thinks by default: ~50 s instead of ~3 s
        }
        req = urllib.request.Request(self.url, json.dumps(body).encode(), {"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=60) as r:
            if on_action is None:
                return json.load(r)["choices"][0]["message"]["content"]
            out, seen = "", 0
            for line in r:
                if cancel is not None and cancel.is_set():
                    raise TimeoutError("cancelled")
                line = line.decode().strip()
                if not line.startswith("data: ") or line == "data: [DONE]":
                    continue
                ch = json.loads(line[6:]).get("choices") or [{}]
                out += ch[0].get("delta", {}).get("content") or ""
                found = _ACTION_RE.findall(out)
                for name, count in found[seen:]:
                    on_action(name, int(count))
                seen = len(found)
            return out
