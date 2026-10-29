"""Spoken command → plan: two steps (Omni transcribes, then plans from text) vs one step (audio straight into the plan).

usage: uv run python tools/voice_eval.py VOICE_SET_DIR [URL]
VOICE_SET_DIR comes from tools/voice_set.py. Scores plans like tools/brain_eval.py and times every step.
"""

import base64
import json
import re
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tonypi.agent.brain import SCHEMA, SYSTEM  # noqa: E402

DIR = Path(sys.argv[1])
URL = sys.argv[2] if len(sys.argv) > 2 else "http://127.0.0.1:8080/v1/chat/completions"
AUDIO_NOTE = ("\nRequests may arrive as audio. The recording can start with the wake phrase 'Hey Tony' (sometimes after "
              "unrelated talk): act on what is said after it.")
WAKE = re.compile(r"^.*?\b(?:(?:hey|hi|hay|okay|ok|a)[\s,]+)?(?:tony|toni|tonie|toney)\b[\s,.!?]*", re.I | re.S)


def post(body: dict) -> tuple[float, str]:
    t = time.monotonic()
    req = urllib.request.Request(URL, json.dumps(body).encode(), {"Content-Type": "application/json"})
    r = json.load(urllib.request.urlopen(req, timeout=300))
    return time.monotonic() - t, r["choices"][0]["message"].get("content") or ""


def audio_part(f: Path) -> dict:
    return {"type": "input_audio", "input_audio": {"data": base64.b64encode(f.read_bytes()).decode(), "format": "wav"}}


HEARD_SCHEMA = {**SCHEMA, "properties": {"heard": {"type": "string"}, **SCHEMA["properties"]},
                "required": ["heard", *SCHEMA["required"]]}
HEARD_NOTE = ' First write in "heard" exactly what was said after the wake phrase, then plan from it.'


def plan_body(system: str, user, schema=SCHEMA) -> dict:
    return {"messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "max_tokens": 200, "temperature": 0.1, "cache_prompt": True,
            "response_format": {"type": "json_schema", "json_schema": {"name": "plan", "schema": schema}},
            "chat_template_kwargs": {"enable_thinking": False}}


def score(raw: str, c: dict) -> tuple[bool, str]:
    try:
        d = json.loads(raw)
        acts = [(a["action"], a.get("count", 1)) for a in d.get("actions", [])]
        say = d.get("say", "")
    except Exception:
        return False, f"bad JSON {raw[:60]!r}"
    names = [a for a, _ in acts]
    ok = (names in c["accept"] and (c["say_re"] is None or re.search(c["say_re"], say or "", re.I))
          and all(dict(acts).get(k, 0) >= v for k, v in c["counts"].items()))
    return ok, f"{acts} say={say!r}"


def main() -> None:
    cases = [c for c in json.loads((DIR / "manifest.json").read_text()) if c["kind"] in ("cmd", "real")]
    post(plan_body(SYSTEM, "hello"))  # warm both prompt caches
    post(plan_body(SYSTEM + AUDIO_NOTE, "hello"))
    res = {"two-step": [], "one-step": [], "one+heard": []}
    post(plan_body(SYSTEM + AUDIO_NOTE + HEARD_NOTE, "hello", HEARD_SCHEMA))
    for c in cases:  # all two-step first, then all one-step: each keeps its cached system prompt in its own slot
        f = DIR / c["file"]
        t_asr, text = post({"messages": [{"role": "system", "content": "Perform ASR in English."},
                                         {"role": "user", "content": [audio_part(f)]}],
                            "max_tokens": 80, "temperature": 0, "cache_prompt": False,
                            "chat_template_kwargs": {"enable_thinking": False}})
        m = WAKE.match(text)
        cmd = text[m.end():].strip() if m else text.strip()
        t_plan, raw = post(plan_body(SYSTEM, cmd))
        ok, info = score(raw, c)
        res["two-step"].append((c, ok, t_asr + t_plan, f"asr {t_asr:.1f}s + plan {t_plan:.1f}s | heard {cmd!r} -> {info}"))
    for c in cases:
        t, raw = post(plan_body(SYSTEM + AUDIO_NOTE, [audio_part(DIR / c["file"])]))
        ok, info = score(raw, c)
        res["one-step"].append((c, ok, t, f"{t:.1f}s -> {info}"))
    for c in cases:
        t, raw = post(plan_body(SYSTEM + AUDIO_NOTE + HEARD_NOTE, [audio_part(DIR / c["file"])], HEARD_SCHEMA))
        ok, info = score(raw, c)
        heard = json.loads(raw).get("heard", "?") if raw.startswith("{") else "?"
        res["one+heard"].append((c, ok, t, f"{t:.1f}s heard {heard!r} -> {info}"))
    if "--only-heard" in sys.argv:
        res = {"one+heard": res["one+heard"]}
    for mode, rows in res.items():
        for kind in ("cmd", "real"):
            sub = [r for r in rows if r[0]["kind"] == kind]
            if not sub:
                continue
            ts = sorted(r[2] for r in sub)
            print(f"{mode:8} {kind:4}: {sum(r[1] for r in sub)}/{len(sub)} correct | median {ts[len(ts) // 2]:.1f}s, "
                  f"max {ts[-1]:.1f}s", flush=True)
        for c, ok, _, info in rows:
            if not ok or c["kind"] == "real":
                print(f"    {'ok' if ok else 'x '} {c['text']!r}: {info}", flush=True)


if __name__ == "__main__":
    main()
