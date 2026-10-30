"""Autonomy scheduler policy with a fake host (no robot, no Codex)."""

import asyncio
import fcntl
import os
import time

import pytest

from tonypi.brain import store
from tonypi.voice import autonomy as A


@pytest.fixture(autouse=True)
def brain(tmp_path, monkeypatch):
    monkeypatch.setenv("TONYPI_BRAIN_DIR", str(tmp_path / "brain"))
    monkeypatch.setenv("TONYPI_LIVE_DIR", str(tmp_path / "live"))
    return tmp_path / "brain"


GOOD = {"battery_mv": 12100, "hottest": [6, "l_elbow", 45], "stop_latched": False, "tether": {"on": False}}


class Host:
    def __init__(self):
        self.quiet, self.busy, self.body_task, self.running = 1000.0, False, False, False
        self.tokens, self.rl, self.state = 0, None, dict(GOOD)
        self.started, self.ended = [], []
        self.start_delay = 0.0

    def quiet_s(self):
        return self.quiet

    def l_busy(self):
        return self.busy

    def body_task_running(self):
        return self.body_task

    def session_running(self):
        return self.running

    async def start_session(self, goal, objective, body_ok, budget_tokens):
        if self.start_delay:
            await asyncio.sleep(self.start_delay)
        self.started.append((goal["id"], objective, body_ok, budget_tokens))
        self.running = True
        return f"thread-{len(self.started)}"

    async def end_session(self, reason):
        self.ended.append(reason)
        self.running = False

    def tokens_used_today(self):
        return self.tokens

    async def rate_limits(self):
        return self.rl

    def tonyd_state(self):
        return self.state


class Clock:
    def __init__(self):
        self.t = time.time()

    def __call__(self):
        return self.t


def make():
    h, c = Host(), Clock()
    return h, c, A.Autonomy(h, clock=c)


def tick(a):
    return asyncio.run(a.tick())


def test_gates_before_start():
    h, c, a = make()
    h.quiet = 10
    assert tick(a) == "not quiet"
    h.quiet, h.busy = 1000, True
    assert tick(a) == "L busy"
    h.busy, h.body_task = False, True
    assert tick(a) == "body task running"
    h.body_task = False
    store.set_autonomy(last_session_end=c.t - 30)
    assert tick(a) == "cooldown"
    store.set_autonomy(last_session_end=0, paused_until=c.t + 600)
    assert tick(a) == "paused"
    store.set_autonomy(paused_until=0, tokens_today=A.DAILY_TOKENS)
    assert tick(a) == "daily token budget used"
    store.set_autonomy(tokens_today=0)
    h.rl = {"rateLimits": {"primary": {"usedPercent": 71}, "secondary": {"usedPercent": 10}}}
    assert tick(a).startswith("primary rate limit")
    h.rl = {"rateLimits": {"primary": {"usedPercent": 10}, "secondary": {"usedPercent": 60}}}
    assert tick(a).startswith("secondary rate limit")
    assert h.started == []


def test_run_now_skips_quiet_and_cooldown():
    h, c, a = make()
    h.quiet = 0
    store.set_autonomy(run_now=True, last_session_end=c.t)
    assert tick(a) == "started g1"
    assert store.autonomy_state()["run_now"] is False


def test_start_session_contract_and_bookkeeping():
    h, c, a = make()
    store.note_goal("g1", "swept the left ankle")
    assert tick(a) == "started g1"
    gid, objective, body_ok, budget = h.started[0]
    assert gid == "g1" and body_ok and budget == A.SESSION_TOKENS
    assert "Learn my body" in objective and "Done when:" in objective and "Next step:" in objective
    assert "swept the left ankle" in objective and "tony goal" in objective
    assert len(objective) <= A.OBJECTIVE_MAX
    g = store.get_goal("g1")
    assert g["attempts"] == 1 and g["last_worked"]
    st = store.autonomy_state()
    assert st["sessions_today"] == 1 and st["goal_sessions"] == {"g1": 1}
    assert any("[autonomy] start g1" in x for x in store.journal_lines())
    assert tick(a) == "running g1"
    # the session finishes on its own: tokens counted, cooldown starts
    h.tokens, h.running = 120_000, False
    c.t += 300
    assert tick(a) == "session finished"
    st = store.autonomy_state()
    assert st["tokens_today"] == 120_000 and st["last_session_end"] == c.t
    assert any("[autonomy] end g1 (finished, 300 s, 120k tokens" in x for x in store.journal_lines())
    assert tick(a) == "cooldown"


def test_body_gates_pick_non_body_goal():
    h, c, a = make()
    for state, why in ((dict(GOOD, battery_mv=10900), "battery"), (dict(GOOD, hottest=[6, "x", 70]), "servo 70"),
                       (dict(GOOD, stop_latched=True), "stop latched"), (None, "tonyd unreachable"),
                       (dict(GOOD, battery_mv=None), "battery unknown")):
        ok, reason = A.body_check(state, {}, c.t)
        assert not ok and why in reason
    assert A.body_check(dict(GOOD, battery_mv=None, tether={"on": True}), {}, c.t)[0]
    assert not A.body_check(GOOD, {"body_paused_until": c.t + 5}, c.t)[0]
    h.state = dict(GOOD, battery_mv=10500)
    assert tick(a) == "started g6"
    gid, objective, body_ok, _ = h.started[0]
    assert not body_ok and "Body: NOT available (battery 10.50 V)" in objective


def test_owner_speech_preempts():
    h, c, a = make()
    tick(a)
    asyncio.run(a.on_user_activity())
    assert h.ended == ["owner spoke"] and a.session is None
    assert any("end g1 (owner spoke" in x for x in store.journal_lines())
    asyncio.run(a.on_user_activity())  # nothing running: no-op
    assert h.ended == ["owner spoke"]


def test_owner_speech_while_starting():
    h, c, a = make()
    h.start_delay = 0.05

    async def go():
        t = asyncio.create_task(a.tick())
        await asyncio.sleep(0.01)
        await a.on_user_activity()
        return await t

    assert asyncio.run(go()) == "started g1"
    assert h.started and h.ended == ["owner spoke"] and a.session is None


def test_time_cap_pause_and_stop():
    h, c, a = make()
    tick(a)
    c.t += A.SESSION_MAX_S
    assert tick(a) == "time cap: session ended" and h.ended == ["time cap"]
    c.t += A.COOLDOWN_S
    assert tick(a) == "started g1"
    store.set_autonomy(paused_until=c.t + 60)
    assert tick(a) == "paused: session ended"
    store.set_autonomy(paused_until=0)
    c.t += A.COOLDOWN_S
    tick(a)
    asyncio.run(a.on_stop())
    assert h.ended[-1] == "stop" and store.autonomy_state()["body_paused_until"] >= c.t + 1700
    c.t += A.COOLDOWN_S
    assert tick(a) == "started g6"  # body paused: only the non-body goal


def test_a_session_that_finished_by_itself_is_still_cleaned_up():
    """Goal complete / idle: the host pauses a still-active goal, interrupts the turn and stops the body."""
    h, c, a = make()
    tick(a)
    h.running = False
    assert tick(a) == "session finished" and h.ended == ["finished"] and a.session is None


def test_l_busy_ends_a_running_session_and_rechecks_before_start():
    h, c, a = make()
    tick(a)
    h.busy = True  # an L turn: the owner is being served
    assert tick(a) == "L busy: session ended" and h.ended == ["owner spoke"]
    h.busy = False
    c.t += A.COOLDOWN_S

    async def rl_then_owner_speaks():  # the owner speaks while the rate limits are read
        h.quiet = 1
        return None
    h.rate_limits = rl_then_owner_speaks
    assert tick(a) == "not quiet" and len(h.started) == 1


def test_owner_speech_during_start_found_by_l_busy():
    h, c, a = make()
    real = h.start_session

    async def start(*args):
        h.busy = True  # the owner's request reached L while the thread was starting
        return await real(*args)
    h.start_session = start
    assert tick(a) == "started g1" and h.ended == ["owner spoke"] and a.session is None


def test_robot_lock_blocks_body_work():
    h, c, a = make()
    fd = os.open(A.ROBOT_LOCK, os.O_RDWR | os.O_CREAT)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)  # Claude Code or a test holds it
        assert A.robot_lock_holder() == "other"
        assert tick(a) == "started g6"  # only the non-body goal
        assert not h.started[0][2] and "robot lock held" in h.started[0][1]
        asyncio.run(a.end("test"))
        fcntl.flock(fd, fcntl.LOCK_UN)
        c.t += A.COOLDOWN_S
        assert tick(a) == "started g1" and h.started[1][2]  # a body session
        os.pwrite(fd, b"lab 123\n", 0)  # its own learning batch holds the lock: keeps running
        fcntl.flock(fd, fcntl.LOCK_EX)
        assert A.robot_lock_holder() == "lab" and tick(a) == "running g1"
        os.ftruncate(fd, 0)  # someone else takes it mid-session: the body session ends
        assert tick(a) == "robot lock held: session ended" and h.ended[-1] == "robot lock held"
    finally:
        os.close(fd)
    assert A.robot_lock_holder() == ""


def test_start_failure_sets_cooldown():
    h, c, a = make()

    async def boom(*_):
        raise RuntimeError("codex down")

    h.start_session = boom
    assert tick(a) == "started g1"
    assert a.session is None and store.autonomy_state()["last_session_end"] == c.t
    assert tick(a) == "cooldown"


def test_curriculum_then_self_review():
    h, c, a = make()
    for i in range(1, 7):
        store.update_goal(f"g{i}", state="dropped" if i != 1 else "done")
    assert tick(a) == "started g7"  # joint_map done -> next rung becomes a self goal
    g = store.get_goal("g7")
    assert g["rung"] == "weight_shift" and g["origin"] == "self" and g["needs_body"] and g["state"] == "active"
    asyncio.run(a.end("test"))
    store.update_goal("g7", state="dropped")
    h.state = None  # no body: the evergreen self-review goal
    c.t += A.COOLDOWN_S
    assert tick(a) == "started g8"
    assert store.get_goal("g8")["kind"] == "self-review"
    asyncio.run(a.end("test"))
    assert store.get_goal("g8")["state"] == "paused"
    c.t += A.COOLDOWN_S
    assert tick(a) == "started g8"  # re-activated, not duplicated
    assert len(store.list_goals()) == 8


def test_per_goal_daily_cap():
    h, c, a = make()
    store.set_autonomy(goal_sessions={"g1": A.GOAL_SESSIONS_PER_DAY})
    assert tick(a) == "started g2"


def test_rate_limited_parsing():
    assert A.rate_limited(None) == ""
    assert A.rate_limited({"primary": {"usedPercent": 69.9}}) == ""
    assert A.rate_limited({"ordinaryUsageAllowed": False, "rateLimits": {}}) == "usage not allowed"
    assert "reached" in A.rate_limited({"rateLimits": {"rateLimitReachedType": "primary"}})


def test_status():
    h, c, a = make()
    tick(a)
    c.t += 42
    s = a.status()
    assert s["session"] == {"goal": "g1", "title": store.get_goal("g1")["title"], "s": 42, "body": True}
