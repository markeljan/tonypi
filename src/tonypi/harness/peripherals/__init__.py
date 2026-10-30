"""Peripherals: every capability the brain can use, one small module each.

A peripheral exposes `Tool`s (JSON-schema params + a blocking handler returning a `Result`: text plus optional JPEG
images). The harness serves the same tools to every brain backend (Claude, LFM3, later Codex), so a new peripheral
works everywhere at once.

TODO(peripherals): adding one (gripper, sonar, the iPhone app, ...):
  1. write `peripherals/<name>.py` with a `Peripheral` subclass; `tools()` returns its Tool specs and `prompt` one
     paragraph for the system prompt. Handlers run in a worker thread and may block.
  2. long handlers check `ctx.steer` (set when the user just spoke) and return early at a safe point;
     tools that act on the world set `moves=True` so the harness can hold them back when new words are pending.
  3. add it to REGISTRY below and enable it with TONYPI_PERIPHERALS=body,camera,voice,<name>.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Callable


@dataclass
class Result:
    text: str
    images: list[bytes] = field(default_factory=list)  # JPEG
    error: bool = False


@dataclass
class Ctx:
    """Passed to every handler."""
    steer: threading.Event  # the user said something new: finish at a safe boundary and return
    emit: Callable[..., None]  # emit(type, **fields) -> front-end event


@dataclass
class Tool:
    name: str
    description: str
    handler: Callable[[dict, Ctx], Result]
    params: dict[str, dict] = field(default_factory=dict)  # JSON-schema properties
    required: list[str] = field(default_factory=list)
    moves: bool = False  # acts on the world (motion): not started while new words from the user are pending
    read_only: bool = False  # safe to run in parallel with anything
    ordered: bool = False  # touches the world (body, head, camera): runs one at a time, in the order called

    def schema(self) -> dict:
        return {"type": "object", "properties": self.params, "required": self.required}


class Peripheral:
    name = ""
    prompt = ""  # a paragraph for the system prompt: what this peripheral is and how to use its tools

    def tools(self) -> list[Tool]:
        return []

    def start(self) -> None:
        pass

    def stop(self) -> None:
        """'stop' from the user (no LLM): halt whatever this peripheral is doing, now."""

    def close(self) -> None:
        pass


def build(names: list[str], **deps) -> list[Peripheral]:
    """Instantiate peripherals by name. deps: shared objects some of them need (tonyd client, speaker)."""
    from tonypi.harness import learning
    from tonypi.harness.peripherals import body, camera, iphone, voice

    registry: dict[str, Callable[[], Peripheral]] = {
        "body": lambda: body.BodyPeripheral(deps["tonyd"]),
        "camera": lambda: camera.CameraPeripheral(deps["tonyd"]),
        "voice": lambda: voice.VoicePeripheral(deps.get("speaker"), deps.get("on_say"), deps.get("voicelink"),
                                               deps.get("ask_typed")),
        "iphone": lambda: iphone.IphonePeripheral(),
        "learning": lambda: learning.Learning(),
    }
    out = []
    for n in names:
        if n not in registry:
            raise ValueError(f"unknown peripheral {n!r}; known: {', '.join(registry)}")
        out.append(registry[n]())
    return out
