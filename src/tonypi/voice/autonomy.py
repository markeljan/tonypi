"""Autonomy: Tony works on his goals when nobody talks to him (docs/BRAIN.md).

Pure policy plus a tick loop. The host (LiveBrain in live.py) does the work: start_session runs one persistent Codex
thread with a native goal; this module only decides when, on what, with which budget, and when to stop. The goal
ledger, journal and budgets live in BRAIN_DIR (tonypi.brain.store), so a voice restart only delays the next tick.

Host interface (all cheap; the async ones may talk to Codex or tonyd):
  quiet_s() -> float            seconds since the last user speech or L activity
  l_busy() -> bool              L (the voice brain) has a turn running
  body_task_running() -> bool   a background task is using the body
  session_running() -> bool     the autonomy session thread is still working (False once its goal ended or it idles)
  await start_session(goal, objective, body_ok, budget_tokens) -> str   (thread id)
  await end_session(reason)     every end, also a session that finished by itself: pause the goal if still
                                active, interrupt the turn, stop the body (no latch), mark the learning-runner preempt
  tokens_used_today() -> int    tokens counted by the host (autonomy usage is the delta over a session)
  await rate_limits() -> dict | None   account/rateLimits/read result
  tonyd_state() -> dict | None
"""

from __future__ import annotations

import asyncio
import fcntl
import logging
import os
import time

from tonypi.brain import store

log = logging.getLogger("tonypi.voice.autonomy")

TICK_S = 15
IDLE_S = 75  # quiet this long before a session starts
COOLDOWN_S = 90  # between sessions
SESSION_MAX_S = 900  # wall-clock cap per session
# Codex counts cached prompt tokens too: a body session used 399k in 146 s (2026-10-30). The weekly rate limit,
# paced below, is the real governor; these caps only stop a runaway.
SESSION_TOKENS = 1_500_000
DAILY_TOKENS = 25_000_000  # autonomy tokens per day
MAX_PRIMARY_PCT = 70  # rate-limit windows: leave headroom for conversation
MAX_SECONDARY_PCT = 60
PACE_SLACK_PCT = 25  # a window may be used up to (elapsed share of it + this), so autonomy spreads over the week
BODY_MIN_MV = 11_000  # body work needs this much battery (or unknown battery and the charger tether on)
BODY_MAX_C = 70  # hottest servo
GOAL_SESSIONS_PER_DAY = 4  # one goal can't eat the whole day; others (or nothing) get a turn
BODY_PAUSE_AFTER_STOP_S = 1800
OBJECTIVE_MAX = 3900  # Codex goal objectives are limited to 4000 characters
# Claude Code and tests hold it for robot work; the learning runner holds it during a batch (and writes "lab ..." in it).
ROBOT_LOCK = os.environ.get("TONYPI_ROBOT_LOCK", "/run/lock/tonypi-robot.lock")
SELF_REVIEW = dict(
    title="Self-review: improve my own code and memory", kind="self-review", priority=5, needs_body=False,
    why="When the body can't be used, I still get better: fix errors, tidy memory, make learning faster.",
    success="one concrete improvement per session: a tested small commit, a fixed journal error, or curated memory "
            "(MEMORY.md under 4 KB, no duplicates, Recent folded into the right sections)",
    next_step="read today's journal and the voice log for errors and handoff misses; pick the top one and fix it")


def robot_lock_holder(path: str | None = None) -> str:
    """Who holds the shared robot lock: '' (free), 'lab' (the learning runner: possibly this session's own batch) or
    'other' (Claude Code, a test, a script). A non-blocking probe, released at once."""
    try:
        fd = os.open(path or ROBOT_LOCK, os.O_RDONLY)
    except OSError:  # no lock file: nobody holds it
        return ""
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            try:
                head = os.pread(fd, 8, 0)
            except OSError:
                head = b""
            return "lab" if head.startswith(b"lab ") else "other"
        fcntl.flock(fd, fcntl.LOCK_UN)
        return ""
    finally:
        os.close(fd)


def body_check(state: dict | None, st: dict, now: float, lock: str = "") -> tuple[bool, str]:
    """(body_ok, why not). Unknown tonyd state means no body work; so does a held robot lock (`lock`, from
    robot_lock_holder)."""
    if st.get("body_paused_until", 0) > now:
        return False, "body paused"
    if lock:
        return False, "robot lock held" + (" by a learning batch" if lock == "lab" else " (Claude Code or a test)")
    if not state:
        return False, "tonyd unreachable"
    if state.get("stop_latched"):
        return False, "stop latched"
    mv = state.get("battery_mv")
    if mv:
        if mv < BODY_MIN_MV:
            return False, f"battery {mv / 1000:.2f} V"
    elif not (state.get("tether") or {}).get("on"):
        return False, "battery unknown"
    hot = state.get("hottest")
    temp = hot[-1] if isinstance(hot, (list, tuple)) and hot else hot
    if isinstance(temp, (int, float)) and temp >= BODY_MAX_C:
        return False, f"servo {temp} C"
    return True, ""


def rate_limited(rl: dict | None, now: float | None = None) -> str:
    """Why the rate limits forbid a session ('' = fine). Accepts the full account/rateLimits/read result or one
    snapshot; missing data is fine (Codex itself refuses when the account is out)."""
    if not rl:
        return ""
    if rl.get("ordinaryUsageAllowed") is False:
        return "usage not allowed"
    snap = rl.get("rateLimits") or rl
    if snap.get("rateLimitReachedType"):
        return f"rate limit reached ({snap['rateLimitReachedType']})"
    for key, cap in (("primary", MAX_PRIMARY_PCT), ("secondary", MAX_SECONDARY_PCT)):
        win = snap.get(key) or {}
        used = win.get("usedPercent")
        if not isinstance(used, (int, float)):
            continue
        if used >= cap:
            return f"{key} rate limit {used:.0f}%"
        mins, resets = win.get("windowDurationMins"), win.get("resetsAt")
        if isinstance(mins, (int, float)) and mins > 0 and isinstance(resets, (int, float)):
            elapsed = min(1.0, max(0.0, 1 - (resets - (now if now is not None else time.time())) / (mins * 60)))
            if used >= elapsed * 100 + PACE_SLACK_PCT:
                return f"{key} rate limit {used:.0f}% ahead of pace ({elapsed * 100:.0f}% of the window elapsed)"
    return ""


def objective(goal: dict, body_ok: bool, why_no_body: str = "") -> str:
    """The Codex goal objective for a session on `goal`."""
    gid = goal["id"]
    lines = [f"Goal {gid} ({goal.get('origin', 'self')}, priority {goal.get('priority', 3)}): {goal['title']}"]
    if goal.get("why"):
        lines.append(f"Why: {goal['why']}")
    if goal.get("success"):
        lines.append(f"Done when: {goal['success']}")
    if goal.get("next_step"):
        lines.append(f"Next step: {goal['next_step']}")
    notes = (goal.get("notes") or [])[-3:]
    if notes:
        lines.append("Recent notes:")
        lines += [f"- {time.strftime('%m-%d %H:%M', time.localtime(n.get('t', 0)))} {n.get('text', '')}" for n in notes]
    lines.append("Body: available (trials with guard_tilt_deg; falls are fine, get_up and continue)." if body_ok else
                 f"Body: NOT available ({why_no_body or 'gates'}): no motion this session; do the non-body part.")
    lines.append(f"Update the ledger with `tony goal ...` as you go: `tony goal note {gid} TEXT` after each result, "
                 f"`tony goal update {gid} --next-step TEXT`, `tony goal done {gid} EVIDENCE` only when the criteria are "
                 f"measured, `tony goal block {gid} REASON` when stuck after 2 different approaches. Lessons: "
                 "`tony remember --kind lesson TEXT`; things to tell the owner: `tony inbox add TEXT`.")
    return "\n".join(lines)[:OBJECTIVE_MAX]


class Autonomy:
    def __init__(self, host, clock=time.time) -> None:
        self.host, self.clock = host, clock
        self.session: dict | None = None  # {goal_id, title, t0, tokens0, body_ok, thread}
        self.last: str = ""  # last tick's decision, for status / logs

    # ---- loop ----------------------------------------------------------------------------------------------------

    async def run(self) -> None:
        while True:
            try:
                why = await self.tick()
                if why != self.last:
                    log.info("autonomy: %s", why)
                    self.last = why
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("autonomy tick failed")
            await asyncio.sleep(TICK_S)

    async def tick(self) -> str:
        """One decision; returns what happened (for logs and tests)."""
        now = self.clock()
        st = store.autonomy_state()
        if self.session:
            s = self.session
            if s.get("starting"):
                return "starting"
            if s.get("ending"):
                return "ending"
            if not self.host.session_running():  # finished by itself: the host still cleans up (goal, turn, body)
                s["ending"] = True
                await self._end_host("finished")
                self._finish("finished")
                return "session finished"
            if self.host.l_busy():  # L has a turn: the owner is being served (backstop for missed speech)
                await self.end("owner spoke")
                return "L busy: session ended"
            if st.get("paused_until", 0) > now:
                await self.end("paused")
                return "paused: session ended"
            if now - s["t0"] >= SESSION_MAX_S:
                await self.end("time cap")
                return "time cap: session ended"
            if s["body_ok"] and st.get("body_paused_until", 0) > now:
                await self.end("body paused")
                return "body paused: session ended"
            if s["body_ok"] and robot_lock_holder() == "other":
                await self.end("robot lock held")
                return "robot lock held: session ended"
            return f"running {s['goal_id']}"
        if st.get("paused_until", 0) > now:
            return "paused"
        run_now = bool(st.get("run_now"))
        if self.host.l_busy():
            return "L busy"
        if self.host.body_task_running():
            return "body task running"
        if not run_now:
            if self.host.quiet_s() < IDLE_S:
                return "not quiet"
            if now - st.get("last_session_end", 0) < COOLDOWN_S:
                return "cooldown"
        if st.get("tokens_today", 0) >= DAILY_TOKENS:
            return "daily token budget used"
        try:
            rl = await self.host.rate_limits()
        except Exception as e:  # unknown limits don't block: Codex refuses by itself when the account is out
            log.warning("rate limits unavailable: %s", e)
            rl = None
        if why := rate_limited(rl):
            return why
        if self.host.l_busy():  # the owner may have spoken while the rate limits were read
            return "L busy"
        if not run_now and self.host.quiet_s() < IDLE_S:
            return "not quiet"
        body_ok, why_no_body = body_check(self.host.tonyd_state(), st, now, robot_lock_holder())
        goal = self.pick(body_ok, st)
        if goal is None:
            if run_now:
                store.set_autonomy(run_now=False)
            return "nothing to do"
        await self.start(goal, body_ok, why_no_body, st, now)
        return f"started {goal['id']}"

    # ---- policy --------------------------------------------------------------------------------------------------

    def pick(self, body_ok: bool, st: dict) -> dict | None:
        """next_goal (owner active, self active, self proposed), else the next curriculum rung as a new self goal,
        else the evergreen non-body self-review goal. Goals that had GOAL_SESSIONS_PER_DAY sessions today wait."""
        spent = {g for g, n in (st.get("goal_sessions") or {}).items() if n >= GOAL_SESSIONS_PER_DAY}
        goal = store.next_goal(body_ok, exclude=spent)
        if goal:
            return goal
        rung = store.next_rung(body_ok)
        if rung:
            gid = store.add_goal(rung["title"], why=f"Next rung of my skill ladder (curriculum: {rung['id']}).",
                                 origin="self", priority=3, success=rung.get("target", ""),
                                 next_step=rung.get("how", ""), needs_body=rung.get("needs_body", True),
                                 state="active", rung=rung["id"])
            if gid not in spent:
                return store.get_goal(gid)
        review = next((g for g in store.list_goals(store.OPEN) if g.get("kind") == "self-review"), None)
        if review is None:
            gid = store.add_goal(SELF_REVIEW["title"], origin="self", state="active",
                                 **{k: v for k, v in SELF_REVIEW.items() if k != "title"})
            review = store.get_goal(gid)
        if review["id"] in spent:
            return None
        if review.get("state") != "active":
            review = store.update_goal(review["id"], state="active")
        return review

    async def start(self, goal: dict, body_ok: bool, why_no_body: str, st: dict, now: float) -> None:
        gid = goal["id"]
        budget = max(10_000, min(SESSION_TOKENS, DAILY_TOKENS - st.get("tokens_today", 0)))
        sessions = dict(st.get("goal_sessions") or {})
        sessions[gid] = sessions.get(gid, 0) + 1
        store.set_autonomy(run_now=False, sessions_today=st.get("sessions_today", 0) + 1, goal_sessions=sessions)
        fields = {"attempts": int(goal.get("attempts") or 0) + 1, "last_worked": round(now)}
        if goal.get("state") == "proposed":
            fields["state"] = "active"
        goal = store.update_goal(gid, **fields)
        store.journal("autonomy", f"start {gid} ({'body' if body_ok else 'no body: ' + why_no_body}, "
                                  f"budget {budget // 1000}k): {goal['title']}")
        self.session = {"goal_id": gid, "title": goal["title"], "t0": now, "tokens0": self._tokens(),
                        "body_ok": body_ok, "starting": True}
        try:
            thread = await self.host.start_session(goal, objective(goal, body_ok, why_no_body), body_ok, budget)
        except Exception as e:
            log.exception("autonomy session failed to start")
            self.session.pop("starting", None)
            self._finish(f"start failed: {e}")
            return
        s = self.session
        s.pop("starting", None)
        s["thread"] = thread
        if not s.get("preempted") and self.host.l_busy():
            s["preempted"] = "owner spoke"
        if s.get("preempted"):  # the owner spoke (or stop) while it was starting
            await self._end_host(s["preempted"])
            self._finish(s["preempted"])

    def _tokens(self) -> int:
        try:
            return int(self.host.tokens_used_today() or 0)
        except Exception:
            return 0

    # ---- ending --------------------------------------------------------------------------------------------------

    async def end(self, reason: str) -> None:
        """End the running session (if any): the host pauses its goal, interrupts it and stops the body."""
        s = self.session
        if s is None:
            return
        if s.get("starting"):  # start() finishes the job once start_session returns
            s.setdefault("preempted", reason)
            return
        if s.get("ending"):  # already ending (speech events come in bursts)
            return
        s["ending"] = True
        await self._end_host(reason)
        self._finish(reason)

    async def _end_host(self, reason: str) -> None:
        try:
            await self.host.end_session(reason)
        except Exception:
            log.exception("autonomy end_session failed")

    def _finish(self, reason: str) -> None:
        s, self.session = self.session, None
        if s is None:
            return
        now = self.clock()
        used = max(0, self._tokens() - s["tokens0"])
        st = store.autonomy_state()
        store.set_autonomy(last_session_end=now, tokens_today=st.get("tokens_today", 0) + used)
        g = store.get_goal(s["goal_id"])
        if g and g.get("kind") == "self-review" and g.get("state") == "active":
            store.update_goal(g["id"], state="paused")  # evergreen: picked again only as the fallback
        store.journal("autonomy", f"end {s['goal_id']} ({reason}, {int(now - s['t0'])} s, {used // 1000}k tokens)"
                                  + (f", goal now {g['state']}" if g else ""))
        store.commit(f"autonomy: {s['goal_id']} {reason}")

    async def on_user_activity(self) -> None:
        """The owner spoke: give him the robot and the brain back now."""
        if self.session:
            await self.end("owner spoke")

    async def on_stop(self) -> None:
        """The stop reflex fired: end the session and keep the body still for BODY_PAUSE_AFTER_STOP_S (the host
        clears body_paused_until when the owner asks for a move)."""
        store.set_autonomy(body_paused_until=self.clock() + BODY_PAUSE_AFTER_STOP_S)
        await self.end("stop")

    def status(self) -> dict:
        st = store.autonomy_state()
        s = self.session
        return {"session": None if s is None else {"goal": s["goal_id"], "title": s["title"],
                                                     "s": int(self.clock() - s["t0"]), "body": s["body_ok"]},
                "last": self.last, "paused_until": st.get("paused_until", 0),
                "body_paused_until": st.get("body_paused_until", 0), "tokens_today": st.get("tokens_today", 0),
                "sessions_today": st.get("sessions_today", 0)}
