"""Switch Tony's brain (harness) by voice or command: "switch to pi", "switch to Hermes", "switch to our own".

One harness runs at a time as tonypi-brain@<name>.service (tools/e2e.sh <name>); tonypi-switch@<name>.service
(tools/switch.sh) stops the running one and starts the next, detached from whoever asked (the voice service
restarts during a switch). "off" / "local" goes back to the plain tonypi-agent.
"""

from __future__ import annotations

import re
import subprocess

# speech recognition mangles both words ("Tony, which Khermes"), so match loosely
_VERB = r"(?:switch|switched|which|swish|twitch|change|swap)(?:\s+(?:over|back))?(?:\s+(?:to|too|two))?"
_NAME = r"(k?h?ermes|herpes|herm[eè]s|hermis|our own|own|custom|codex|gpt live|live|alive|life|voice brain|pie|pi|py|p i|local|default)"
SWITCH_RE = re.compile(rf"\b{_VERB}\s+(?:the\s+)?{_NAME}\b|\buse\s+(?:the\s+)?{_NAME}\s+(?:harness|brain|agent)\b", re.I)


def _canon(name: str) -> str:
    n = name.lower()
    if "erm" in n or "herpes" in n:
        return "hermes"
    if n in ("our own", "own", "custom", "codex"):
        return "own"
    if n in ("local", "default"):
        return "off"
    if n in ("gpt live", "live", "alive", "life", "voice brain"):
        return "live"
    return "pi"


LABEL = {"own": "our own harness", "hermes": "Hermes", "pi": "pi", "live": "live mode", "off": "the local agent"}


def parse_switch(text: str) -> str | None:
    m = SWITCH_RE.search(text or "")
    return None if not m else _canon(m.group(1) or m.group(2))


def request_switch(name: str) -> None:
    """Asynchronous: the caller may be stopped by the switch itself."""
    subprocess.Popen(["sudo", "systemctl", "start", "--no-block", f"tonypi-switch@{name}.service"])


def current() -> str:
    for name in ("own", "hermes", "pi", "live"):
        if subprocess.run(["systemctl", "is-active", "-q", f"tonypi-brain@{name}"]).returncode == 0:
            return name
    return "off"
