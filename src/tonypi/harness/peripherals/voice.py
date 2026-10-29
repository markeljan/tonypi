"""Voice out: Piper through the speaker, spoken in order on its own thread so the brain never waits for it."""

from __future__ import annotations

import logging
import queue
import re
import threading
from typing import Callable

from tonypi.harness.peripherals import Ctx, Peripheral, Result, Tool

log = logging.getLogger(__name__)


def clean(text: str) -> str:
    """Markdown and emoji out: it is read aloud."""
    text = re.sub(r"[*_#`>|]", "", text)
    text = re.sub(r"[\U0001F000-\U0001FAFF☀-➿]", "", text)
    return re.sub(r"\s+", " ", text).strip()


class VoicePeripheral(Peripheral):
    name = "voice"
    prompt = ("Voice: everything you write as plain text is spoken aloud right away, so write like you talk: one or "
              "two short sentences, no markdown, lists or emojis. say() speaks too (same thing, as a tool).")

    def __init__(self, speaker=None, on_say: Callable[[str], None] | None = None) -> None:
        self.speaker = speaker  # tonypi.agent.speech.Speaker, or None (muted: events only)
        self.on_say = on_say
        self._q: queue.Queue[str] = queue.Queue()
        threading.Thread(target=self._loop, name="voice", daemon=True).start()

    def tools(self) -> list[Tool]:
        return [Tool("say", "Speak out loud now (non-blocking).", self._say_tool,
                     {"text": {"type": "string"}}, ["text"])]

    def _say_tool(self, args: dict, ctx: Ctx) -> Result:
        self.speak(str(args.get("text", "")))
        return Result("speaking")

    def speak(self, text: str) -> None:
        text = clean(text)
        if not text:
            return
        if self.on_say:
            self.on_say(text)
        self._q.put(text)

    def idle(self) -> bool:
        return self._q.empty() and not (self.speaker and self.speaker.speaking.is_set())

    def wait_idle(self, timeout: float = 30) -> None:
        import time
        end = time.monotonic() + timeout
        while not self.idle() and time.monotonic() < end:
            time.sleep(0.05)

    def stop(self) -> None:
        while True:
            try:
                self._q.get_nowait()
            except queue.Empty:
                break
        if self.speaker:
            self.speaker.stop()

    def _loop(self) -> None:
        while True:
            text = self._q.get()
            if self.speaker:
                try:
                    self.speaker.say(text)
                except Exception:
                    log.exception("speech failed")
