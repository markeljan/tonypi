"""Turns a request into a plan: what to say + which skill-level actions to run.

The local LLM (llama-server, OpenAI-compatible) answers with JSON constrained by a schema whose action names are an
enum, so it can only pick from ACTIONS. A few deterministic rules run around it: `stop` never reaches the LLM,
vision questions always use the camera, and a move named outright in the request is never dropped.
"""

from __future__ import annotations

import json
import re
import urllib.request
from dataclasses import dataclass, field

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
    + "\nDo exactly what was asked: no extra moves. If asked something you can't do, say so briefly.\n"
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
    (re.compile(r"\bnod\b", re.I), "nod"),
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


def _clean_say(text: str) -> str:
    return re.sub(r"[*_#`]", "", text or "").strip()


def postprocess(request: str, plan: Plan) -> Plan:
    acts: list[tuple[str, int]] = []
    for name, count in plan.actions:
        if name not in ACTIONS:
            continue
        count = max(1, min(5, int(count)))
        if acts and acts[-1][0] == name:  # the 1.2B model likes to repeat the same move
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


class Brain:
    def __init__(self, url: str = "http://127.0.0.1:8080/v1/chat/completions", history_turns: int = 3) -> None:
        self.url = url
        self.history: list[dict] = []
        self.history_turns = history_turns

    def plan(self, request: str) -> Plan:
        request = request.strip()
        if STOP_RE.match(request):
            return Plan([("stop", 1)], "", "rule")
        try:
            raw = self._llm(request)
            data = json.loads(raw)
            plan = Plan([(a["action"], a.get("count", 1)) for a in data.get("actions", [])], data.get("say", ""))
            self.history += [{"role": "user", "content": request}, {"role": "assistant", "content": raw}]
            self.history = self.history[-2 * self.history_turns:]
        except Exception as e:  # LLM down or bad JSON: rules only
            plan = Plan([], "", f"fallback ({type(e).__name__})")
        plan = postprocess(request, plan)
        if not plan.actions and not plan.say:
            plan.say = "Sorry, I didn't understand that."
        return plan

    def _llm(self, request: str) -> str:
        body = {
            "messages": [{"role": "system", "content": SYSTEM}, *self.history, {"role": "user", "content": request}],
            "max_tokens": 200,
            "temperature": 0.1,
            "cache_prompt": True,
            "response_format": {"type": "json_schema", "json_schema": {"name": "plan", "schema": SCHEMA}},
        }
        req = urllib.request.Request(self.url, json.dumps(body).encode(), {"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r)["choices"][0]["message"]["content"]
