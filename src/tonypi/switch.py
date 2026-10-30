"""Switch Tony between his two brains by voice or command: "switch to local" / "switch to live".

live  = tonypi-voice (GPT-Live-1 voice + the Codex brain, src/tonypi/voice/live.py): the default, enabled at boot.
local = tonypi-agent (the offline LFM3 agent): the fallback when the network or the ChatGPT login is down.
The two units conflict (both own the mic and speaker), so starting one stops the other; the start is detached
(--no-block) because the caller is usually the service being stopped. tools/switch.sh does the same from a shell.
The bake-off harnesses (own, Hermes, pi) are retired to branches deprecated/harness-*: "own" / "our own" now mean live,
"Hermes" and "pi" match nothing.
"""

from __future__ import annotations

import re
import subprocess

# speech recognition mangles both words ("Tony, which to alive"), so match loosely
_VERB = r"(?:switch|switched|which|swish|twitch|change|swap)(?:\s+(?:over|back))?(?:\s+(?:to|too|two))?"
# ("off" is not a spoken name: "switch off the light" must not switch brains; tools/switch.sh takes it)
_NAME = r"(our own|own|codex|gpt live|live|alive|life|voice brain|local|default)"
SWITCH_RE = re.compile(rf"\b{_VERB}\s+(?:the\s+)?{_NAME}\b|\buse\s+(?:the\s+)?{_NAME}\s+(?:harness|brain|agent)\b", re.I)

UNITS = {"live": "tonypi-voice", "local": "tonypi-agent"}
LABEL = {"live": "live mode", "local": "the local agent"}


def _canon(name: str) -> str:
    return "local" if name.lower() in ("local", "default", "off") else "live"


def parse_switch(text: str) -> str | None:
    """"live", "local" or None."""
    m = SWITCH_RE.search(text or "")
    return None if not m else _canon(m.group(1) or m.group(2))


def request_switch(target: str) -> None:
    """Asynchronous: the caller may be stopped by the switch itself (the units conflict)."""
    subprocess.Popen(["sudo", "systemctl", "start", "--no-block", f"{UNITS[target]}.service"])


def current() -> str | None:
    """Which brain is running ("live" / "local"), or None when neither is."""
    for name, unit in UNITS.items():
        if subprocess.run(["systemctl", "is-active", "-q", unit]).returncode == 0:
            return name
    return None
