"""tonypi-hermes: Tony's always-on front end with Hermes Agent as the brain.

Peripherals (voice, the terminal socket, later the iPhone app) turn what people say into text and hand it to
`Front.submit`. Hermes thinks and acts through the tonypi MCP server; this process only routes text and speaks:
- "stop" (and friends) never reaches the LLM: tonyd stops the body at once, speech is cut, the Hermes run is killed.
- Anything said while Hermes is working steers the running turn (Hermes /v1/runs/{id}/steer): the skill running
  now is stopped first, so the correction lands as soon as the model sees the cancelled result, not after a long
  walk. A steer that came too late (the model was already writing its answer) is replayed as the next turn.
- Hermes' final reply, and commentary it writes between tool calls, is spoken; tonyd warnings are spoken when idle.
Brain tiers: simple commands go to FAST (gpt-6.1-terra, no reasoning), open-ended or multi-step tasks ("find the door",
"go to the kitchen", "think hard ...") to DEEP (gpt-6.1-sol, medium reasoning); TONYPI_HERMES_TIER forces one.
A Hermes session holds the conversation ("again", "that one" work); after IDLE_S without a command the next one starts
a fresh session, so requests stay small (the local fallback has an 8K window). Lasting facts live in Hermes memory.
"""

from __future__ import annotations

import argparse
import itertools
import logging
import os
import queue
import re
import threading
import time
from typing import Callable

from tonypi.agent.brain import STOP_RE
from tonypi.hermes.client import HermesError, HermesRuns
from tonypi.tonyd.client import TonydClient, TonydError

log = logging.getLogger("tonypi.hermes")
SESSION = os.environ.get("TONYPI_HERMES_SESSION", "tonypi")  # prefix; the session id is SESSION-<start time>
IDLE_S = float(os.environ.get("TONYPI_HERMES_IDLE_S", "300"))
# (model, reasoning effort) per tier; provider is the Hermes provider slug both run on
PROVIDER = os.environ.get("TONYPI_HERMES_PROVIDER", "openai-codex")
TIERS = {
    "fast": (os.environ.get("TONYPI_HERMES_MODEL", "gpt-6.1-terra"), os.environ.get("TONYPI_HERMES_EFFORT", "none")),
    "deep": (os.environ.get("TONYPI_HERMES_DEEP_MODEL", "gpt-6.1-sol"),
             os.environ.get("TONYPI_HERMES_DEEP_EFFORT", "medium")),
}
TIER = os.environ.get("TONYPI_HERMES_TIER", "auto")  # auto | fast | deep
DEEP_RE = re.compile(r"\b(find|search|look(ing)? for|explore|go (to|into|and)|walk (to|toward|towards|over)|bring|fetch|"
                     r"get me|figure out|plan|navigate|follow|think (hard|carefully)|big brain)\b", re.I)


def tier_for(text: str) -> str:
    return TIER if TIER in TIERS else "deep" if DEEP_RE.search(text) else "fast"


class Front:
    def __init__(self, hermes: HermesRuns, tonyd: TonydClient | None = None, speak: Callable[[str], None] | None = None,
                 stop_speaking: Callable[[], None] | None = None, session_id: str = SESSION) -> None:
        self.hermes = hermes
        self.tonyd = tonyd if tonyd is not None else TonydClient(on_event=self._on_tonyd_event)
        self._speak, self._stop_speaking = speak, stop_speaking
        self.session_prefix = session_id
        self.session_id = ""
        self.last_turn = 0.0  # monotonic end of the last turn
        self.listeners: list[Callable[[dict], None]] = []  # peripherals' event sinks
        self.on_answered: list[Callable[[str], None]] = []  # called with the source when a turn ends normally
        self.ids = itertools.count(1)
        self.lock = threading.Lock()
        self.run_id: str | None = None  # the Hermes run in flight
        self.rids: list[int] = []  # submissions folded into it (first + steers)
        self.source = "terminal"
        self.stopped = False
        self.speech: queue.Queue = queue.Queue()
        threading.Thread(target=self._speech_loop, name="speech", daemon=True).start()

    # ---- events / speech --------------------------------------------------------------------------------------

    def emit(self, type_: str, **kw) -> None:
        ev = {"type": type_, "t": round(time.time(), 2), **kw}
        log.info("%s %s", type_, kw)
        for f in list(self.listeners):
            try:
                f(ev)
            except Exception:
                log.exception("event listener failed")

    def say(self, text: str, rid: int | None = None) -> None:
        text = text.strip()
        if text:
            self.emit("say", id=rid, text=text)
            self.speech.put(text)

    def hush(self) -> None:
        while not self.speech.empty():
            try:
                self.speech.get_nowait()
            except queue.Empty:
                break
        if self._stop_speaking:
            self._stop_speaking()

    def _speech_loop(self) -> None:
        while True:
            text = self.speech.get()
            if self._speak:
                try:
                    self._speak(text)
                except Exception:
                    log.exception("speaking failed")

    def _on_tonyd_event(self, ev: dict) -> None:
        if ev.get("event") == "warn":  # low battery / overheating, from tonyd's watchdog
            self.emit("info", text=ev["text"])
            if self.run_id is None:
                self.say(ev["text"])

    # ---- commands ---------------------------------------------------------------------------------------------

    def submit(self, text: str, source: str = "terminal") -> int:
        rid = next(self.ids)
        text = text.strip()
        self.emit("heard", id=rid, source=source, text=text)
        if not text:
            self.emit("done", id=rid)
            return rid
        if STOP_RE.match(text):
            self.stop_all()
            self.emit("action", id=rid, name="stop", count=1)
            self.emit("done", id=rid)
            return rid
        deadline = time.monotonic() + 15
        while self.run_id == "starting" and time.monotonic() < deadline:  # POST /v1/runs in flight
            time.sleep(0.05)
        with self.lock:
            run_id = self.run_id
            if run_id is not None:
                self.rids.append(rid)
                self.source = source
        if run_id is not None:
            self._body_stop()  # the newest command wins: cut the skill now so the steer is read right away
            self.hush()
            if self.hermes.steer(run_id, text):
                self.emit("info", id=rid, text="steering the running task")
                return rid
            with self.lock:  # the run just ended: it becomes a new turn
                if rid in self.rids:
                    self.rids.remove(rid)
        self._start(text, [rid], source)
        return rid

    def stop_all(self) -> None:
        """No LLM involved: body, speech, then the Hermes run."""
        self._body_stop()
        self.hush()
        with self.lock:
            run_id, self.stopped = self.run_id, self.run_id is not None
        if run_id:
            self.hermes.stop(run_id)

    def _body_stop(self) -> None:
        try:
            self.tonyd.stop()
        except TonydError as e:
            log.warning("stop: %s", e)

    def _start(self, text: str, rids: list[int], source: str) -> None:
        with self.lock:
            busy = self.run_id is not None
            if not busy:
                self.run_id, self.rids, self.source, self.stopped = "starting", list(rids), source, False
        if busy:  # lost a race with another submit: steer into that one instead
            for rid in rids:
                self.emit("done", id=rid)
            self.submit(text, source)
            return
        threading.Thread(target=self._turn, args=(text,), name="hermes-turn", daemon=True).start()

    def _turn(self, text: str) -> None:
        t0 = time.monotonic()
        rid = self.rids[0]
        self.emit("thinking", id=rid)
        pending = ""
        if not self.session_id or time.monotonic() - self.last_turn > IDLE_S:
            self.session_id = f"{self.session_prefix}-{time.strftime('%Y%m%d-%H%M%S')}"
            self.emit("info", id=rid, text=f"new Hermes session {self.session_id}")
        tier = tier_for(text)
        model, effort = TIERS[tier]
        self.emit("info", id=rid, text=f"brain: {model} ({effort} reasoning)")
        try:
            run_id = self.hermes.start(text, self.session_id, model, PROVIDER if model else "", effort)
            with self.lock:
                self.run_id = run_id
                stop_now = self.stopped
            if stop_now:
                self.hermes.stop(run_id)
            first_tool = None
            for ev in self.hermes.events(run_id):
                kind = ev.get("event", "")
                if kind == "tool.started":
                    if first_tool is None:
                        first_tool = time.monotonic() - t0
                    name = str(ev.get("tool", "")).removeprefix("mcp_tonypi_")
                    self.emit("action", id=rid, name=name, count=1, preview=ev.get("preview", ""),
                              seconds=round(time.monotonic() - t0, 1))
                elif kind == "message.interim" and ev.get("text"):
                    self.say(ev["text"], rid)  # "Looking for the door." between tool calls
                elif kind in ("run.completed", "run.failed", "run.cancelled", "run.interrupted"):
                    pending = (ev.get("pending_steer") or "").strip()
                    if kind == "run.completed" and not pending:
                        self.say(ev.get("output") or "", rid)
                    elif kind == "run.failed":
                        self.emit("info", id=rid, text=f"hermes: {ev.get('error', 'run failed')}")
                        if not self.stopped:
                            self.say("Sorry, my brain had a problem with that.", rid)
                    self.emit("info", id=rid, text=f"{kind} in {time.monotonic() - t0:.1f} s"
                              + (f", first tool at {first_tool:.1f} s" if first_tool is not None else "")
                              + (f", usage {ev['usage']}" if ev.get("usage") else "")
                              + (f", runtime {ev['runtime']}" if ev.get("runtime") else ""))
        except HermesError as e:
            log.error("%s", e)
            self.emit("info", id=rid, text=str(e))
            self.say("I can't reach my brain right now.", rid)
        finally:
            self.last_turn = time.monotonic()
            with self.lock:
                rids, source, stopped = self.rids, self.source, self.stopped
                self.run_id, self.rids = None, []
        if pending and not stopped:  # a steer the turn ended before reading: it's the next turn
            self._start(pending, rids, source)
            return
        for r in rids:
            self.emit("done", id=r)
        if not stopped:
            for f in self.on_answered:
                f(source)

    def status(self) -> dict:
        return self.tonyd.state(fresh=True)

    @property
    def busy(self) -> bool:
        return self.run_id is not None


def main() -> None:
    # voice: streaming transcripts only unless TONYPI_ASR_URL is set (LFM3 re-transcription is off while the cloud
    # brain is the only model in use); read by tonypi.agent.speech at import
    os.environ.setdefault("TONYPI_ASR_URL", "")
    from tonypi.hermes import peripherals

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--no-voice", action="store_true", help="terminal socket only (no mic, no speaker)")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    speaker = None if args.no_voice else peripherals.make_speaker()
    front = Front(HermesRuns(), speak=speaker.say if speaker else None, stop_speaking=speaker.stop if speaker else None)
    for p in peripherals.enabled(front, speaker):
        p.start()
    if not front.hermes.health():
        log.warning("Hermes API server not reachable at %s (start: tonypi gateway run)", front.hermes.url)
    front.say("Tony is ready." + ("" if args.no_voice else " Say hey Tony, then your command."))
    threading.Event().wait()


if __name__ == "__main__":
    main()
