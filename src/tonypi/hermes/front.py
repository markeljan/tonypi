"""tonypi-hermes: Tony's always-on front end with Hermes Agent as the brain.

Peripherals (voice, the terminal socket, later the iPhone app) turn what people say into text and hand it to
`Front.submit`. Hermes thinks and acts through the tonypi MCP server; this process only routes text and speaks:
- "stop" (and friends) never reaches the LLM: tonyd stops the body at once, speech is cut, the Hermes run is killed.
- Anything said while Hermes is working steers the running turn (Hermes /v1/runs/{id}/steer): the skill running
  now is stopped first, so the correction lands as soon as the model sees the cancelled result, not after a long
  walk. A steer that came too late (the model was already writing its answer) is replayed as the next turn.
- Hermes' final reply, and commentary it writes between tool calls, is spoken; tonyd warnings are spoken when idle.
- Falls and posture are this harness's job (tonyd no longer guards tilt or settles): keeper.BodyKeeper settles after
  body moves and gets Tony up after a fall, without an LLM, and tells the running Hermes turn.
- Tasks persist: a DEEP task keeps going ("not done yet, try another way" turns in the same session) until Tony
  reports it with tonypi-self's task_done, "stop" is said, or TASK_CAP_S runs out. Every task is logged as one JSON
  line (TASK_LOG): timings, tool calls, motions/skills created, falls, retries, model, tokens, how it was done.
- Self-update: tonypi-self's self_reload schedules a reload; when idle this process re-execs itself (harness code)
  or restarts the Hermes gateway (config). SOUL and skills apply on the next turn without either.
Brain tiers: short known commands ("wave", "what do you see?") go to FAST (gpt-6.1-terra, no reasoning); everything
else (walk to / find / new motions / learn / multi-step) to DEEP (gpt-6.1-sol, medium); TONYPI_HERMES_TIER forces one.
A Hermes session holds the conversation ("again", "that one" work); after IDLE_S without a command the next one starts
a fresh session, so requests stay small (the local fallback has an 8K window). Lasting facts live in Hermes memory.
"""

from __future__ import annotations

import argparse
import itertools
import json
import logging
import os
import queue
import re
import sys
import threading
import time
from pathlib import Path
from typing import Callable

from tonypi.agent.brain import STOP_RE
from tonypi.hermes.client import HermesError, HermesRuns
from tonypi.hermes.keeper import BodyKeeper
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
                     r"get me|figure out|plan|navigate|follow|think (hard|carefully)|big brain|learn|improve|fix|"
                     r"new|make up|invent|then|and then|until)\b", re.I)
# one known move or a quick question: no planning needed
FAST_RE = re.compile(r"^\W*(please\W+)?(wave|bow|nod|shake your head|dance|do a dance|kung fu|squat|twist|flap|arms up|"
                     r"stand( up)?|lie down|get up|wake up|go to sleep|sleep|look (left|right|up|down|ahead|straight)|"
                     r"turn (left|right)|walk (forward|backward|back)|step (left|right)|march in place|kick|punch|"
                     r"what do you see|what can you see|how('?s| is) your battery|how hot|are you ok)\b", re.I)
TASK_CAP_S = float(os.environ.get("TONYPI_HERMES_TASK_CAP_S", "600"))  # E2E test: 10 minutes per task
MAX_CONTINUES = int(os.environ.get("TONYPI_HERMES_MAX_CONTINUES", "12"))
TETHER_RESET = os.environ.get("TONYPI_HERMES_TETHER_RESET", "0") == "1"  # E2E procedure: reset at each task start
STATE = Path(os.environ.get("TONYPI_HERMES_STATE", Path.home() / ".local/state/tonypi/hermes"))
TASK_LOG = STATE / "tasks.jsonl"
CONTINUE_PROMPT = ("[harness] The task \"{task}\" is not reported done yet (attempt {n}). Check where things stand "
                   "(camera, get_state, last results). If the same thing failed twice, don't repeat it: follow "
                   "when-stuck (gather data, say what's wrong, try a genuinely different way) or ask_user. Keep going; "
                   "call task_done when it is done, or task_done(success=false) only if you have truly run out of "
                   "options.")


def tier_for(text: str) -> str:
    if TIER in TIERS:
        return TIER
    short = len(text.split()) <= 9
    return "fast" if short and FAST_RE.search(text) and not DEEP_RE.search(text) else "deep"


def read_report(since: float) -> dict | None:
    """tonypi-self's task_done record, if written after `since` (wall clock)."""
    try:
        r = json.loads((STATE / "task_report.json").read_text())
    except (OSError, ValueError):
        return None
    return r if r.get("t", 0) >= since else None


class Front:
    def __init__(self, hermes: HermesRuns, tonyd: TonydClient | None = None, speak: Callable[[str], None] | None = None,
                 stop_speaking: Callable[[], None] | None = None, session_id: str = SESSION) -> None:
        self.hermes = hermes
        self.tonyd = tonyd if tonyd is not None else TonydClient(on_event=self._on_tonyd_event)
        self._speak, self._stop_speaking = speak, stop_speaking
        self.keeper = None  # keeper.BodyKeeper, set by main()
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
        self.task: dict | None = None  # the task being worked on (its log record)
        self._asking: dict | None = None  # a pending ask(): {"done": Event, "answer"}
        self.speaking = threading.Event()
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

    def say(self, text: str, rid: int | None = None, final: bool = False) -> None:
        """final: the answer to request `rid` (the voice service speaks one final reply per request); else progress."""
        text = text.strip()
        if text:
            self.emit("say", id=rid, text=text, final=final)
            self.speech.put(text)

    def ask(self, question: str, timeout_s: float = 30) -> str | None:
        """Ask the user out loud (voice service) and on the terminal; the first answer, spoken or typed, wins."""
        waiter = {"done": threading.Event(), "answer": None}
        self._asking = waiter
        self.emit("info", text=f"[asking] {question}")
        self.emit("ask", text=question, timeout_s=timeout_s)
        waiter["done"].wait(timeout_s)
        self._asking = None
        self.emit("info", text=f"[answer] {waiter['answer']!r}")
        return waiter["answer"]

    def answer(self, text: str) -> bool:
        """An answer to the pending ask (False if nothing is being asked)."""
        waiter = self._asking
        if waiter is None or waiter["done"].is_set():
            return False
        waiter["answer"] = text
        waiter["done"].set()
        return True

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
            self.speaking.set()
            try:
                if self._speak:
                    self._speak(text)
            except Exception:
                log.exception("speaking failed")
            finally:
                if self.speech.empty():
                    self.speaking.clear()

    def _on_tonyd_event(self, ev: dict) -> None:
        if ev.get("event") == "job" and self.keeper is not None:
            self.keeper.on_job(ev)
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
            if self._asking is not None:
                self.answer(text)
            self.stop_all()
            self.emit("action", id=rid, name="stop", count=1)
            self.emit("done", id=rid)
            return rid
        if self.answer(text):  # Tony asked something; this is the reply, not a new command
            self.emit("info", id=rid, text="taken as the answer to Tony's question")
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
        if self.keeper is not None:
            self.keeper.note_stop()
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
        threading.Thread(target=self._task, args=(text,), name="hermes-task", daemon=True).start()

    def _task(self, text: str) -> None:
        """One task: turns until done (task_done), stopped, out of time, or (FAST) after one turn."""
        rid, source = self.rids[0], self.source
        tier = tier_for(text)
        model, effort = TIERS[tier]
        t0, wall0 = time.monotonic(), time.time()
        task = self.task = {"id": f"{time.strftime('%Y%m%d-%H%M%S')}-{rid}", "text": text, "source": source,
                            "tier": tier, "model": model, "effort": effort, "received": round(wall0, 2),
                            "first_action_s": None, "tool_calls": [], "turns": 0, "retries": 0, "steers": 0,
                            "tokens_in": 0, "tokens_out": 0, "runs": [], "rid": rid}
        self.emit("thinking", id=rid)
        self.emit("info", id=rid, text=f"brain: {model} ({effort} reasoning)")
        if not self.session_id or time.monotonic() - self.last_turn > IDLE_S or tier == "deep":
            self.session_id = f"{self.session_prefix}-{time.strftime('%Y%m%d-%H%M%S')}"  # a task gets a clean session
            self.emit("info", id=rid, text=f"new Hermes session {self.session_id}")
        task["session"] = self.session_id
        if TETHER_RESET:
            try:
                self.tonyd.call("tether", reset=True)
            except TonydError as e:
                log.warning("tether reset: %s", e)
        before = self._authored()
        falls0 = len(self.keeper.events) if self.keeper is not None else 0
        prompt, report = text, None
        watchdog = threading.Timer(TASK_CAP_S, self._time_up, args=(task,))
        watchdog.daemon = True
        watchdog.start()
        while True:
            turn_wall = time.time()
            out = self._turn(prompt, rid, model, effort, t0, task)
            task["turns"] += 1
            if self.stopped:
                break
            if out["pending"]:  # a steer the turn ended before reading: same task, next turn
                prompt, task["steers"] = out["pending"], task["steers"] + 1
                continue
            report = read_report(turn_wall)
            if report or tier == "fast" or out["kind"] == "error":
                self.say(out["output"], rid, final=True)
                break
            if time.monotonic() - t0 > TASK_CAP_S or task["retries"] >= MAX_CONTINUES:
                self.say(out["output"] + " I'm out of time for this one.", rid, final=True)
                task["timed_out"] = True
                break
            self.say(out["output"], rid)  # progress; the task goes on
            task["retries"] += 1
            prompt = CONTINUE_PROMPT.format(task=text, n=task["retries"] + 1)
            with self.lock:
                self.run_id = "starting"  # between turns: new words wait for the next run and steer it
        watchdog.cancel()
        with self.lock:
            rids, stopped = self.rids, self.stopped
            self.run_id, self.rids = None, []
        self.last_turn = time.monotonic()
        self._finish_log(task, t0, report, stopped, before, falls0)
        threading.Thread(target=self._commit_learned, args=(task,), daemon=True).start()
        for r in rids:
            self.emit("done", id=r)
        if not stopped:
            for f in self.on_answered:
                f(source)

    def _turn(self, text: str, rid: int, model: str, effort: str, t0: float, task: dict) -> dict:
        """One Hermes run. Returns {kind, output, pending}."""
        out = {"kind": "error", "output": "", "pending": ""}
        tr = time.monotonic()
        try:
            run_id = self.hermes.start(text, self.session_id, model, PROVIDER if model else "", effort)
            with self.lock:
                self.run_id = run_id
                stop_now = self.stopped
            if stop_now:
                self.hermes.stop(run_id)
            for ev in self.hermes.events(run_id):
                kind = ev.get("event", "")
                if kind == "tool.started":
                    now = round(time.monotonic() - t0, 1)
                    if task["first_action_s"] is None:
                        task["first_action_s"] = now
                    name = str(ev.get("tool", "")).removeprefix("mcp__tonypi__").removeprefix("mcp__tonypi-self__")
                    task["tool_calls"].append([now, name, str(ev.get("preview", ""))[:120]])
                    self.emit("action", id=rid, name=name, count=1, preview=ev.get("preview", ""), seconds=now)
                elif kind == "message.interim" and ev.get("text"):
                    self.say(ev["text"], rid)  # "Looking for the door." between tool calls
                elif kind in ("run.completed", "run.failed", "run.cancelled", "run.interrupted"):
                    out.update(kind=kind, pending=(ev.get("pending_steer") or "").strip(), output=ev.get("output") or "")
                    if kind == "run.failed":
                        self.emit("info", id=rid, text=f"hermes: {ev.get('error', 'run failed')}")
                        out["output"] = "" if self.stopped else "Sorry, my brain had a problem with that."
                    usage = ev.get("usage") or {}
                    task["tokens_in"] += int(usage.get("input_tokens") or 0)
                    task["tokens_out"] += int(usage.get("output_tokens") or 0)
                    task["runs"].append({"run": run_id, "kind": kind, "s": round(time.monotonic() - tr, 1)})
                    self.emit("info", id=rid, text=f"{kind} in {time.monotonic() - tr:.1f} s"
                              + (f", usage {usage}" if usage else ""))
        except HermesError as e:
            log.error("%s", e)
            self.emit("info", id=rid, text=str(e))
            out["output"] = "I can't reach my brain right now."
        return out

    def _time_up(self, task: dict) -> None:
        """The task cap (TASK_CAP_S) passed in the middle of a turn: stop it like "stop", but say why."""
        if self.task is not task:
            return
        log.warning("task cap %.0f s reached: stopping", TASK_CAP_S)
        task["timed_out"] = True
        self.stop_all()
        self.say("I'm out of time for this one.", task.get("rid"), final=True)

    def _commit_learned(self, task: dict) -> None:
        """Skills Tony refined during the task (skill_manage) are committed on the branch (tonypi-self)."""
        try:
            from tonypi.hermes.self_mcp import commit_learned

            if head := commit_learned(task["text"]):
                self.emit("info", text=f"learned and committed: {head}")
        except Exception as e:  # git or files: the task itself is over and logged
            log.warning("learning commit: %s", e)

    def _authored(self) -> set[str]:
        """User motions and skills in tonyd (to log what a task created)."""
        try:
            skills = self.tonyd.call("skills").get("details", {})
            motions = self.tonyd.call("motions")["motions"]
        except (TonydError, KeyError, AttributeError):
            return set()
        return ({f"skill:{k}" for k, v in skills.items() if v.get("source") == "user"}
                | {f"motion:{m['name']}" for m in motions if m.get("source") == "user"})

    def _finish_log(self, task: dict, t0: float, report: dict | None, stopped: bool, before: set[str],
                    falls0: int) -> None:
        deadline = time.monotonic() + 60  # "done" = the answer has been spoken
        while (self.speaking.is_set() or not self.speech.empty()) and time.monotonic() < deadline:
            time.sleep(0.1)
        created = sorted(self._authored() - before)
        body = self.keeper.events[falls0:] if self.keeper is not None else []
        task.update(done_s=round(time.monotonic() - t0, 1), stopped=stopped,
                    success=(report or {}).get("success"), summary=(report or {}).get("summary"),
                    how=(report or {}).get("how"), created=created,
                    falls=sum(1 for e in body if e["kind"] == "fall"),
                    recoveries=sum(1 for e in body if e["kind"] == "get_up"), body_events=body)
        try:
            STATE.mkdir(parents=True, exist_ok=True)
            with TASK_LOG.open("a") as f:
                f.write(json.dumps(task) + "\n")
        except OSError as e:
            log.warning("task log: %s", e)
        self.emit("info", text=f"task logged: {task['id']} success={task['success']} "
                  f"{task['done_s']} s, {len(task['tool_calls'])} tools, retries {task['retries']}, created {created}")
        self.task = None

    def body_note(self, note: str) -> None:
        """Something happened to the body (a fall): the running Hermes turn hears it right after its current tool."""
        self.emit("info", text=note)
        run_id = self.run_id
        if run_id not in (None, "starting"):
            self.hermes.steer(run_id, f"[Tony's body, not the user] {note}")

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
    ap.add_argument("--voice-service", action="store_true",
                    help="attach to tonypi-voice (GPT-Live-1) as the active brain; only in our own robot slot "
                         "(hermes/start.sh passes it): the last attach wins")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    service = args.voice_service and not args.no_voice  # tonypi-voice owns the mic and speaker (GPT-Live-1)
    if service and not peripherals.voice_service_up():
        log.warning("--voice-service: %s not answering yet; will keep trying", peripherals.VOICE_SOCK)
    speaker = None if args.no_voice or service else peripherals.make_speaker()
    front = Front(HermesRuns(), speak=speaker.say if speaker else None, stop_speaking=speaker.stop if speaker else None)
    front.keeper = BodyKeeper(front.tonyd, front.say, on_fall=front.body_note)
    front.keeper.start()
    for p in peripherals.enabled(front, speaker, voice_service=service):
        p.start()
    if not front.hermes.health():
        log.warning("Hermes API server not reachable at %s (start: tonypi gateway run)", front.hermes.url)
    front.say("Tony is ready." + ("" if args.no_voice else " Say hey Tony, then your command."))
    watch_reload(front)


SOURCES = Path(__file__).resolve().parent
GATEWAY_RESTART = os.environ.get("TONYPI_HERMES_GATEWAY_RESTART", "systemctl --user restart tonypi-hermes-gw")


def watch_reload(front: Front) -> None:
    """Self-update without a manual restart: when idle, re-exec this process if its code changed (or tonypi-self asked
    for it), and restart the Hermes gateway if asked (config). Never in the middle of a task."""
    import subprocess

    def mtimes() -> dict:
        return {p: p.stat().st_mtime for p in SOURCES.glob("*.py")}

    code = mtimes()
    reload_file = STATE / "reload.json"
    seen = reload_file.stat().st_mtime if reload_file.exists() else 0.0
    while True:
        time.sleep(2)
        if front.busy or front.speaking.is_set():
            continue
        want: dict = {}
        if reload_file.exists() and reload_file.stat().st_mtime > seen:
            seen = reload_file.stat().st_mtime
            try:
                want = json.loads(reload_file.read_text())
            except ValueError:
                want = {}
            reload_file.unlink(missing_ok=True)
        if "gateway" in want:
            log.warning("self-update: restarting the Hermes gateway")
            subprocess.run(GATEWAY_RESTART, shell=True, check=False)
        if "front" in want or mtimes() != code:
            log.warning("self-update: harness code changed, restarting the front end")
            front.say("Reloading my new code.")
            deadline = time.monotonic() + 10
            while (front.speaking.is_set() or not front.speech.empty()) and time.monotonic() < deadline:
                time.sleep(0.1)
            os.execv(sys.executable, [sys.executable, "-m", "tonypi.hermes.front", *sys.argv[1:]])


if __name__ == "__main__":
    main()
