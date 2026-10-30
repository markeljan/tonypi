"""Voice: what Tony says goes to the shared tonypi-voice service (GPT-Live-1) when a VoiceLink is attached, else to
Piper through the local speaker (in order, on its own thread, so the brain never waits). Also asking the user."""

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
    prompt = ("Voice: everything you write as plain text, and the 'say' of your status, is spoken aloud right away, "
              "so write like you talk: one or two short sentences, no markdown, lists or emojis. say() speaks too "
              "(a progress note while you work). ask_user(question) asks the person out loud and returns their "
              "answer: use it when you're stuck or something is unclear (\"I'm stuck at the rug edge, what should I "
              "do?\"), then act on the answer. Asking is good; silently looping or giving up is not.")

    def __init__(self, speaker=None, on_say: Callable[[str], None] | None = None, link=None,
                 ask_typed: Callable[[str, float], str | None] | None = None) -> None:
        self.speaker = speaker  # tonypi.agent.speech.Speaker, or None (muted: events only)
        self.link = link  # VoiceLink to the shared tonypi-voice service (wins over the local speaker)
        self.ask_typed = ask_typed  # no voice service: ask on the text socket, the next typed line answers
        self.on_say = on_say
        self._q: queue.Queue[str] = queue.Queue()
        threading.Thread(target=self._loop, name="voice", daemon=True).start()

    def tools(self) -> list[Tool]:
        return [Tool("say", "Speak out loud now (non-blocking).", self._say_tool,
                     {"text": {"type": "string"}}, ["text"]),
                Tool("ask_user", "Ask the person a question out loud and wait for their answer (up to timeout_s, "
                     "default 30). Returns the answer, or that nobody answered.", self._ask_tool,
                     {"question": {"type": "string"}, "timeout_s": {"type": "number", "minimum": 5, "maximum": 120}},
                     ["question"])]

    def _ask_tool(self, args: dict, ctx: Ctx) -> Result:
        q, timeout = str(args.get("question", "")).strip(), float(args.get("timeout_s") or 30)
        if not q:
            return Result("ask what?", error=True)
        if self.on_say:
            self.on_say(q)
        ctx.emit("ask", text=q)
        if self.link is not None and self.link.connected.is_set():
            answer = self.link.ask(q, timeout)
        elif self.ask_typed is not None:
            self.speak(q)
            answer = self.ask_typed(q, timeout)
        else:
            self.speak(q)
            answer = None
        ctx.emit("answer", text=answer)
        return Result(f"the user answered: {answer!r}" if answer else
                      f"no answer within {timeout:.0f} s: decide yourself (or ask again later)")

    def _say_tool(self, args: dict, ctx: Ctx) -> Result:
        self.speak(str(args.get("text", "")))
        return Result("speaking")

    def speak(self, text: str, final: bool = False) -> None:
        text = clean(text)
        if not text:
            return
        if self.on_say:
            self.on_say(text)
        if self.link is not None and self.link.connected.is_set():
            self.link.speak(text, final)
            return
        self._q.put(text)

    def idle(self) -> bool:
        return self._q.empty() and not (self.speaker and self.speaker.speaking.is_set())

    def wait_idle(self, timeout: float = 30) -> None:
        import time
        end = time.monotonic() + timeout
        while not self.idle() and time.monotonic() < end:
            time.sleep(0.05)

    def task_ended(self) -> None:
        """Close the voice requests the finished task answered (the service waits for a final reply)."""
        if self.link is not None:
            self.link.task_ended()

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
