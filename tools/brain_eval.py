"""Score a model's raw robot plans (no postprocess rules) on a fixed command set.

usage: uv run python tools/brain_eval.py URL LABEL [--think]
  URL is an OpenAI-compatible chat endpoint, e.g. http://127.0.0.1:8080/v1/chat/completions
Prints accuracy, latency, and every miss. Uses the agent's real system prompt and JSON schema.
"""

import json
import re
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tonypi.agent.brain import SCHEMA, SYSTEM  # noqa: E402

A = lambda *x: list(x)  # noqa: E731
# (request, acceptable action-name sequences, regex the spoken reply must match, {action: min count})
CASES = [
    ("dance", [A("dance")], None, {}),
    ("wave at us", [A("wave")], None, {}),
    ("take a bow", [A("bow")], None, {}),
    ("show me some kung fu then take a bow", [A("kung_fu", "bow")], None, {}),
    ("walk forward three steps", [A("walk_forward")], None, {"walk_forward": 3}),
    ("walk forward two steps and turn left", [A("walk_forward", "turn_left")], None, {"walk_forward": 2}),
    ("look left", [A("look_left")], None, {}),
    ("look around the room", [A("look_left", "look_right"), A("look_left", "look_right", "look_center"),
                              A("look_right", "look_left", "look_center"), A("look_right", "look_left")], None, {}),
    ("what do you see?", [A("describe_view")], None, {}),
    ("how's your battery?", [A("report_status")], None, {}),
    ("go to sleep", [A("sleep")], None, {}),
    ("wake up", [A("get_up")], None, {}),
    ("nod if you agree with me", [A("nod")], None, {}),
    ("shake your head", [A("shake_head")], None, {}),
    ("kick with your right foot", [A("kick_right")], None, {}),
    ("do a squat", [A("squat")], None, {}),
    ("raise both your arms", [A("arms_up")], None, {}),
    ("punch with your left hand", [A("punch_left")], None, {}),
    ("step to the right", [A("step_right")], None, {}),
    ("march in place", [A("step_in_place")], None, {}),
    ("back up two steps", [A("walk_backward")], None, {"walk_backward": 2}),
    ("turn around", [A("turn_left"), A("turn_right")], None, {}),
    ("flap your wings like a bird", [A("flap_wings")], None, {}),
    ("dance and then lie down", [A("dance", "lie_down")], None, {}),
    ("twist your hips", [A("twist")], None, {}),
    ("what's 12 times 7?", [A()], r"\b84\b|eighty[- ]four", {}),
    ("what's the capital of Japan?", [A()], r"tokyo", {}),
    ("how many legs does a spider have?", [A()], r"\b8\b|eight", {}),
    ("tell me a short joke", [A()], None, {}),
    ("can you fly?", [A()], r"\b(no|can't|cannot|can not)\b", {}),
]


def ask(q: str, url: str, think: bool):
    body = {"messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": q}],
            "max_tokens": 800 if think else 200, "temperature": 0.1, "cache_prompt": True,
            "response_format": {"type": "json_schema", "json_schema": {"name": "plan", "schema": SCHEMA}},
            "chat_template_kwargs": {"enable_thinking": think}}
    t = time.monotonic()
    req = urllib.request.Request(url, json.dumps(body).encode(), {"Content-Type": "application/json"})
    r = json.load(urllib.request.urlopen(req, timeout=600))
    return time.monotonic() - t, r["choices"][0]["message"]["content"]


def main() -> None:
    url, label, think = sys.argv[1], sys.argv[2], "--think" in sys.argv
    ask("hello", url, think)  # warm the prompt cache
    ok, times, misses = 0, [], []
    for q, accept, say_re, counts in CASES:
        try:
            dt, raw = ask(q, url, think)
        except Exception as e:  # e.g. llama.cpp issue 31328: HTTP 500 with thinking off + response_format
            dt, raw = 0.0, f"ERROR {e}"
        times.append(dt)
        try:
            d = json.loads(raw)
            acts = [(a["action"], a.get("count", 1)) for a in d.get("actions", [])]
            say = d.get("say", "")
        except Exception:
            acts, say = None, raw
        names = None if acts is None else [a for a, _ in acts]
        good = (names in accept and (say_re is None or re.search(say_re, say or "", re.I))
                and all(dict(acts or []).get(k, 0) >= v for k, v in counts.items()))
        ok += bool(good)
        if not good:
            misses.append(f"    x {q!r}: {acts} say={say!r}")
    times.sort()
    print(f"{label}: {ok}/{len(CASES)} correct | latency median {times[len(times) // 2]:.1f}s, "
          f"p90 {times[int(len(times) * 0.9)]:.1f}s, max {times[-1]:.1f}s", flush=True)
    print("\n".join(misses), flush=True)


if __name__ == "__main__":
    main()
