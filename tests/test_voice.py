"""Voice service + live brain without audio, Codex, tonyd or the brain store (fakes only)."""
import asyncio
import json
import os
import re
import time

import numpy as np
import pytest

from tonypi.voice import live, service


class FakeStore:
    def __init__(self):
        self.calls = []
        self.state = {}

    def __getattr__(self, name):
        def call(*a, **kw):
            self.calls.append((name, a, kw))
            if name == "memory_snapshot":
                return "MEMORY-SNAPSHOT"
            if name == "autonomy_state":
                return dict(self.state)
            if name == "set_autonomy":
                self.state.update(kw)
        return call


class FakeAutonomy:
    def __init__(self):
        self.session, self.calls = None, []

    async def on_user_activity(self):
        self.calls.append("user")

    async def on_stop(self):
        self.calls.append("stop")


@pytest.fixture
def mk(tmp_path, monkeypatch):
    """make a LiveBrain whose Codex RPCs, speech and memory are recorded."""
    monkeypatch.setattr(live, "DATA", tmp_path)
    store = FakeStore()
    monkeypatch.setattr(live, "_brain_store", lambda: store)

    def make(results=None):
        hub = type("Hub", (), {"audio": type("A", (), {"voice_until": 0.0})(), "switched": [],
                               "on_live_delegation": lambda self, t: None,
                               "request_brain_switch": lambda self, t: self.switched.append(t)})()
        brain = live.LiveBrain(hub)
        brain.tid, brain.calls, brain.spoken, brain.store = "L", [], [], store

        async def rpc(method, params=None, timeout=None):
            brain.calls.append((method, params))
            if results and method in results:
                return results[method](params)
            if method in ("thread/start", "thread/fork"):
                return {"thread": {"id": f"thread{len(brain.calls)}"}}
            return {"turn": {"id": f"turn{len(brain.calls)}"}}
        brain._rpc = rpc
        brain.speak = lambda text: brain.spoken.append(text) or True
        brain.append = lambda item, text, speak=True: brain.spoken.append(text) or True
        return brain
    return make


def methods(brain):
    return [m for m, _ in brain.calls]


def test_realtime_sessions_disable_automatic_acknowledgement_filler(monkeypatch):
    for cls in (service.Live, live.LiveBrain):
        session = object.__new__(cls)
        session.tid = "test-thread"
        if cls is live.LiveBrain:
            monkeypatch.setattr(cls, "_initial_items", lambda self: [])
        params = session.realtime_params("test-sdp")
        assert params["delegationAckFiller"] is False
        assert params["clientManagedHandoffs"] is (cls is service.Live)
        assert params["prompt"]


def test_realtime_reader_distinguishes_requested_closure_from_errors(caplog):
    async def run(method, params):
        session = service.Live(type("Hub", (), {"audio": None})())
        stream = asyncio.StreamReader()
        stream.feed_data((json.dumps({"method": method, "params": params}) + "\n").encode())
        stream.feed_eof()
        session.proc = type("Proc", (), {"stdout": stream})()
        await session._reader(asyncio.Queue())
        assert session.voice_closed.is_set()
        assert session.closed.is_set()

    import logging
    caplog.set_level(logging.INFO, logger=service.log.name)
    asyncio.run(run("thread/realtime/closed", {"reason": "requested"}))
    assert caplog.records[-1].levelno == logging.INFO
    asyncio.run(run("thread/realtime/closed", {"reason": "disconnected"}))
    assert caplog.records[-1].levelno == logging.WARNING
    asyncio.run(run("thread/realtime/error", {"reason": "requested"}))
    assert caplog.records[-1].levelno == logging.WARNING


def test_live_mode_is_the_default():
    assert service.VOICE_MODE == os.environ.get("TONYPI_VOICE_MODE", "live")


def test_switch_to_local_starts_the_agent_and_switch_to_live_only_acks(monkeypatch):
    switched, said = [], []
    monkeypatch.setattr(service.switch, "parse_switch",
                        lambda t: "off" if "local" in t else "live" if "live" in t else "pi" if "pie" in t else None)
    monkeypatch.setattr(service.switch, "request_switch", switched.append)

    async def run():
        hub = service.Hub(live_enabled=False)
        hub._speak_or_append = lambda item, text: said.append(text)
        hub.on_request("Ciao, switch to the pie", None)  # retired harnesses are not targets any more
        hub.on_live_delegation("switch to live")
        hub.last_switch_t = -1e9
        hub.on_live_delegation("switch to local please")
        await asyncio.sleep(3.2)
    asyncio.run(run())
    assert switched == ["local"]
    assert said[1:] == ["I'm already in live mode.", "Switching to the local agent."]
    assert "offline" in said[0]


def test_bare_stop_stops_the_body_without_any_model(monkeypatch):
    stopped = []
    import tonypi.tonyd.client as tc

    monkeypatch.setattr(tc.TonydClient, "__init__", lambda self, *a, **k: None)
    monkeypatch.setattr(tc.TonydClient, "stop", lambda self, latch=False: stopped.append(latch))

    async def run():
        hub = service.Hub(live_enabled=False)
        hub.on_transcript(" Stop")
        hub.on_transcript(".")
        await asyncio.sleep(0.2)
        hub.last_stop_t = -1e9
        hub.on_request("stop", None)  # local fallback ASR
        await asyncio.sleep(0.2)
    asyncio.run(run())
    assert len(stopped) == 2


def test_socket_ops_without_relay():
    async def run():
        hub = service.Hub(live_enabled=False)
        assert await hub.handle({"op": "ping"}, None) == {"pong": True}
        st = await hub.handle({"op": "status"}, None)
        assert st["voice_mode"] == service.VOICE_MODE and "attached" not in st
        for op in ("attach", "reply", "ask"):
            with pytest.raises(ValueError):
                await hub.handle({"op": op}, None)
    asyncio.run(run())


def test_phone_mic_requests_are_dropped_when_tonys_own_mic_heard_the_user(mk, monkeypatch):
    """A companion phone and Tony's mic often hear the same words; the voice already has its copy. Requests the phone
    alone heard go to the brain labelled, also when they steer a running turn."""
    monkeypatch.setattr(live, "PHONE_WAIT_S", 0.0)
    brain = mk()

    async def go():
        brain.last_user_speech_t = time.monotonic() - 1.0  # Tony's mic heard speech a second ago
        r = await brain.send_text("tony walk forward to steps", "phone_mic")
        assert "dropped" in r and not brain.calls

        brain.last_user_speech_t = time.monotonic() - 60.0  # only the phone heard it
        assert (await brain.send_text("tony wave", "phone_mic"))["turn"]
        method, params = brain.calls[-1]
        assert method == "turn/start" and "iPhone" in params["additionalContext"]["source"]["value"]

        brain.l_turn = "T1"  # a running turn is steered, with the same label
        assert await brain.send_text("the other arm", "phone_mic") == {"steered": "T1"}
        method, params = brain.calls[-1]
        assert method == "turn/steer" and "iPhone" in params["additionalContext"]["source"]["value"]
    asyncio.run(go())
    rows = [json.loads(x) for x in (live.DATA / "conversation.jsonl").read_text().splitlines()]
    assert [r["source"] for r in rows] == ["phone_mic", "phone_mic"]


def test_fragments_are_merged_into_one_bridged_request(mk, monkeypatch):
    monkeypatch.setattr(live, "BRIDGE_QUIET_S", 0.4)
    brain = mk()
    brain.voice_started_t = time.monotonic()  # the session was just woken: speech now is for Tony

    async def go():
        brain.last_user_speech_t = time.monotonic()
        brain.utterance_done("Here's what I want you to do.")
        await asyncio.sleep(0.2)
        brain.last_user_speech_t = time.monotonic()
        brain.utterance_done("Walk to the door")
        await asyncio.sleep(1.0)
    asyncio.run(go())
    turns = [p for m, p in brain.calls if m == "turn/start"]
    assert len(turns) == 1 and "Here's what I want you to do. Walk to the door" in turns[0]["input"][0]["text"]


def test_no_bridge_when_delegated_or_too_short(mk, monkeypatch):
    monkeypatch.setattr(live, "BRIDGE_QUIET_S", 0.2)
    brain = mk()

    async def go():
        brain.utterance_done("wave your hand please")
        brain.last_delegation_t = time.monotonic()  # GPT-Live handed it off after all
        await asyncio.sleep(0.6)
        brain.utterance_done("The first")  # a 2-word fragment
        await asyncio.sleep(0.6)
    asyncio.run(go())
    assert not brain.calls


def test_speech_not_addressed_to_tony_is_not_bridged(mk, monkeypatch):
    monkeypatch.setattr(live, "BRIDGE_QUIET_S", 0.2)
    brain = mk()  # no wake, delegation, typed request or speech of Tony's for minutes: a TV talking

    async def go():
        brain.utterance_done("and the weather tomorrow looks dry")
        await asyncio.sleep(0.6)
        assert not brain.calls
        brain.utterance_done("Tony walk to the kitchen")  # names him: bridged
        await asyncio.sleep(0.6)
    asyncio.run(go())
    assert [m for m, _ in brain.calls] == ["turn/start"]


def test_conversation_log_dedupes_repeats(tmp_path):
    c = live.Conversation(tmp_path / "c.jsonl")
    assert c.add("user", "Hey, are you there?")
    assert not c.add("user", "hey are you there")  # bridge + delegation of the same words
    assert c.add("assistant", "Hey, are you there?")
    assert len(c.tail(10)) == 2


def test_task_ids_survive_restarts(mk):
    a = mk()
    assert re.fullmatch(r"\d{4}-t1", a._next_task_id()) and a._next_task_id().endswith("-t2")
    assert mk()._next_task_id().endswith("-t3")


def test_worker_result_is_spoken_once_logged_and_remembered(mk):
    brain = mk()
    brain.workers["1030-t1"] = {"thread": "w1", "turn": "x", "title": "Tune walk", "brief": "b", "status": "running",
                                "started": time.monotonic(), "t_start": 1, "uses_body": True, "goal": "",
                                "goal_id": "g3", "source": "owner"}
    brain.by_thread["w1"] = "1030-t1"
    brain.last_message["w1"] = json.dumps({
        "status": "done", "spoken_sentence": "Walking is 20 percent faster.", "details": "5 trials each",
        "lesson": "Shorter steps fall less.", "commits": ["abc"], "needs_voice_restart": False, "question": None})
    brain.thread_tokens["w1"] = 1234

    async def go():
        brain.on_notification("turn/completed", {"threadId": "w1", "turn": {"id": "x", "status": "completed"}})
        await asyncio.sleep(0.05)
        await brain._worker_done("1030-t1", {})  # a second report changes nothing
    asyncio.run(go())
    assert brain.spoken == ["Walking is 20 percent faster."]
    row = json.loads((live.DATA / "tasks.jsonl").read_text())
    assert row["id"] == "1030-t1" and row["goal_id"] == "g3" and row["lesson"] and row["tokens"] == 1234
    names = [c[0] for c in brain.store.calls]
    assert "journal" in names and ("remember", ("Shorter steps fall less.",), {"kind": "lesson"}) in brain.store.calls
    inject = [p for m, p in brain.calls if m == "thread/inject_items"][-1]
    assert "do not repeat" in inject["items"][0]["content"][0]["text"]


def test_goal_worker_keeps_going_then_reports(mk):
    brain = mk()

    async def go():
        r = await brain._start_task({"title": "Walk 1 m", "brief": "walk", "uses_body": True, "tier": "sol",
                                     "goal": "1 m straight, 3/3 without falling", "goal_id": "g3"})
        task_id = r.split()[1].rstrip(";")
        start, goal, turn = brain.calls[0][1], brain.calls[1], brain.calls[2][1]
        assert start["ephemeral"] is False and start["serviceName"] == "tonypi-task"
        assert start["model"] == live.MODELS["sol"]
        assert goal[0] == "thread/goal/set" and "3/3" in goal[1]["objective"] and goal[1]["tokenBudget"]
        assert "outputSchema" not in turn and "MEMORY-SNAPSHOT" in turn["additionalContext"]["memory"]["value"]
        thread = brain.workers[task_id]["thread"]
        brain.on_notification("turn/completed", {"threadId": thread, "turn": {"id": "turn3", "status": "completed"}})
        await asyncio.sleep(0.05)
        assert brain.workers[task_id]["status"] == "running" and not brain.spoken  # Codex continues the goal
        brain.on_notification("thread/goal/updated", {"threadId": thread, "goal": {"status": "complete"}})
        await asyncio.sleep(0.05)
        report = [p for m, p in brain.calls if m == "turn/start"][-1]
        assert report["outputSchema"] is live.WORKER_SCHEMA
        brain.last_message[thread] = '```json\n{"status": "done", "spoken_sentence": "I walked one meter."}\n```'
        brain.on_notification("turn/completed", {"threadId": thread, "turn": {"id": "r", "status": "completed"}})
        await asyncio.sleep(0.05)
        assert brain.spoken == ["I walked one meter."] and brain.workers[task_id]["status"] == "completed"
    asyncio.run(go())


def test_autonomy_host_session_lifecycle(mk, monkeypatch):
    brain = mk()
    stopped = []
    monkeypatch.setattr(live.LiveBrain, "_stop_body", staticmethod(lambda: stopped.append(1)))

    async def go():
        assert not brain.session_running() and not brain.l_busy() and not brain.body_task_running()
        assert brain.quiet_s() < 5  # a fresh start isn't "quiet" yet
        thread = await brain.start_session({"id": "g5", "title": "Map the home"}, "Goal g5 ...", False, 400_000)
        start = brain.calls[0][1]
        assert start["serviceName"] == "tonypi-autonomy" and start["ephemeral"] is False
        assert start["config"]["model_reasoning_effort"] == "medium"
        assert "run_skill" in start["config"]["mcp_servers.tonypi.disabled_tools"]
        assert "MEMORY-SNAPSHOT" in start["developerInstructions"]
        assert brain.calls[1] == ("thread/goal/set", {"threadId": thread, "objective": "Goal g5 ...",
                                                      "tokenBudget": 400_000})
        assert brain.session_running()
        brain.on_notification("thread/tokenUsage/updated", {"threadId": thread, "tokenUsage": {
            "total": {"totalTokens": 5000}, "last": {"inputTokens": 4000}}})
        assert brain.tokens_used_today() == 5000
        brain.on_notification("thread/goal/updated", {"threadId": thread, "goal": {"status": "complete"}})
        assert not brain.session_running()
        brain.calls.clear()
        await brain.end_session("finished")  # the scheduler cleans up a session that finished by itself
        assert "thread/goal/set" not in methods(brain)  # complete: nothing to pause; its last turn is interrupted
        assert "turn/interrupt" in methods(brain)
        await asyncio.sleep(0.05)
        row = json.loads((live.DATA / "tasks.jsonl").read_text().splitlines()[-1])
        assert row["source"] == "autonomy" and row["goal_id"] == "g5" and row["tokens"] == 5000
        assert row["status"] == "goal complete"

        brain.calls.clear()  # an idle session with a still-active goal: paused, so Codex can't continue it untracked
        thread = await brain.start_session({"id": "g5", "title": "Map the home"}, "Goal g5 ...", False, 400_000)
        brain.on_notification("turn/completed", {"threadId": thread, "turn": {"id": "t", "status": "completed"}})
        brain.auto["last"] -= live.IDLE_END_S + 1
        assert not brain.session_running()
        await brain.end_session("finished")
        assert ("thread/goal/set", {"threadId": thread, "status": "paused"}) in brain.calls
        assert stopped == []  # no body this session

        brain.calls.clear()  # a body session ended because the owner spoke
        thread = await brain.start_session({"id": "g3", "title": "Walk"}, "Goal g3", True, 100_000)
        assert "mcp_servers.tonypi.disabled_tools" not in brain.calls[0][1]["config"]
        t_end = time.time()
        await brain.end_session("owner spoke")
        assert ("thread/goal/set", {"threadId": thread, "status": "paused"}) in brain.calls
        assert "turn/interrupt" in methods(brain) and stopped == [1] and not brain.session_running()
        from tonypi.brain import preempt_file

        assert float(preempt_file().read_text()) >= t_end - 1  # the session's learning batch stops at its next move

        brain.calls.clear()  # L already serves the owner: no tonyd stop (it could cut L's move off)
        await brain.start_session({"id": "g3", "title": "Walk"}, "Goal g3", True, 100_000)
        brain.l_turn, brain.l_turn_started = "LT", time.monotonic() - 5
        await brain.end_session("owner spoke")
        assert stopped == [1] and float(preempt_file().read_text()) < time.time() - 4  # marked when L's turn began
    asyncio.run(go())


def test_owner_speech_while_the_next_session_starts(mk):
    """The previous session's (closed, ending) dict must not hide the owner from the scheduler."""
    brain = mk()
    brain.autonomy = FakeAutonomy()
    brain.auto = {"thread": "old", "closed": True, "ending": True}
    brain.autonomy.session = {"goal_id": "g1", "starting": True}

    async def go():
        brain.on_dc_event({"type": "input_transcript.added", "item": {"text": "Tony, wave"}})
        await asyncio.sleep(0.01)
        assert brain.autonomy.calls == ["user"]
    asyncio.run(go())


def test_watchdog_survives_a_failed_rpc(mk, monkeypatch):
    async def boom(method, params=None, timeout=None):
        raise RuntimeError(f"{method}: turn already active")

    brain = mk()
    brain._rpc = boom
    brain.l_turn, brain.l_turn_started, brain.last_delegation = "T", time.monotonic() - 60, ("i1", "wave")
    brain.workers["w1"] = {"status": "running", "goal": "x", "turn": None, "idle_t": time.monotonic() - 999,
                           "thread": "W", "title": "x", "uses_body": False}
    reported = []

    async def report(task_id):
        reported.append(task_id)
    brain._report = report

    async def go():
        await brain._watch_once()  # interrupt and the re-run both fail: no exception escapes
        assert brain.l_turn is None and reported == ["w1"]
    asyncio.run(go())


def test_phone_mic_hearing_tonys_own_voice_is_dropped(mk, monkeypatch):
    monkeypatch.setattr(live, "PHONE_WAIT_S", 0.0)
    brain = mk()
    brain.autonomy = FakeAutonomy()
    brain.autonomy.session = {"goal_id": "g1"}
    brain.last_user_speech_t = time.monotonic() - 60.0  # Tony's mic heard nobody

    async def go():
        brain.audio.voice_until = time.monotonic() + 2  # Tony is talking
        r = await brain.send_text("tony I'm going to try walking", "phone_mic")
        assert "talking" in r["dropped"]
        brain.audio.voice_until = time.monotonic() - 30
        brain.convo.add("assistant", "Yeah, I'm Tony, a small humanoid robot.", source="voice")
        r = await brain.send_text("Tony small humanoid robot", "phone_mic")
        assert "matches" in r["dropped"]
        await asyncio.sleep(0.01)
        assert not brain.calls and brain.autonomy.calls == []  # autonomy not ended, nothing reached L
        assert (await brain.send_text("tony wave", "phone_mic"))["turn"]
    asyncio.run(go())


def test_owner_speech_and_stop_reach_autonomy(mk):
    brain = mk()
    brain.autonomy = FakeAutonomy()

    async def go():
        brain.autonomy.session = {"goal_id": "g1"}
        brain.audio.voice_until = time.monotonic() + 5  # Tony is talking: his own echo isn't the owner
        brain.on_dc_event({"type": "input_transcript.added", "item": {"text": "hello"}})
        brain.audio.voice_until = 0.0
        brain.on_dc_event({"type": "input_transcript.added", "item": {"text": "Tony"}})
        await asyncio.sleep(0.01)
        assert brain.autonomy.calls == ["user"]
        await brain.interrupt_all(body_only=True)
        await asyncio.sleep(0.01)
        assert brain.autonomy.calls == ["user", "stop"]
    asyncio.run(go())


def test_a_move_request_clears_the_body_pause(mk):
    brain = mk()
    brain.store.state["body_paused_until"] = time.time() + 1000
    brain._move_asked("ok, now walk forward")
    assert brain.store.state["body_paused_until"] == 0
    for text in ("thanks, that's all", "Don't move.", "What did you learn today?", "stand still", "turn off the light",
                 "no, stay there"):
        brain.store.state["body_paused_until"] = time.time() + 1000
        brain._move_asked(text)
        assert brain.store.state["body_paused_until"] > time.time(), text
    brain._move_asked("Tony, turn left and wave")
    assert brain.store.state["body_paused_until"] == 0


def test_parse_result_tolerates_wrapping():
    assert live.parse_result('noise {"status": "done"} tail') == {"status": "done"}
    assert live.parse_result("plain text") is None


# ---- wake word / idle sleep of the GPT-Live session ------------------------------------------------------------

@pytest.fixture(autouse=True)
def _no_replay_lead(monkeypatch):
    monkeypatch.setattr(service, "REPLAY_LEAD_CHUNKS", 0)


def _chunk(level: int) -> bytes:
    return (np.full(service.MIC_CHUNK, level, np.int16)).tobytes()


def test_held_mic_is_replayed_after_the_session_starts_skipping_long_silence():
    async def run():
        a = service.Audio()
        for _ in range(3):
            a.ring.append(_chunk(5000))  # "hey Tony" just before the spotter fired
        a.start_hold()
        a.mic_q.put_nowait(bytes(640))  # what the starting session got meanwhile: silence
        for level in [0] * 30 + [7000] * 2:
            a.hold.append(_chunk(level))
        a.release_hold()
        assert a.mic_q.qsize() == 1  # the old silence is gone; one chunk wakes a track waiting on the live mic
        out = [np.frombuffer(await a.next_chunk(), np.int16)[0] for _ in range(3 + 10 + 2)]
        assert out == [5000] * 3 + [0] * 10 + [7000] * 2  # 20 of the 30 silent chunks skipped
        a.mic_q.put_nowait(_chunk(42))
        rest = [np.frombuffer(await a.next_chunk(), np.int16)[0] for _ in range(2)]
        assert rest == [0, 42] and a.hold is None  # back to the live mic
    asyncio.run(run())


def test_replay_skips_a_noisy_floor_too():
    async def run():
        a = service.Audio()
        a.start_hold()
        for level in [800] * 40 + [9000] * 3 + [800] * 40:  # max-gain mic: the "silence" is 800
            a.hold.append(_chunk(level))
        a.release_hold()
        a.mic_q.put_nowait(_chunk(1))  # the live mic after the hold
        out = []
        while not out or out[-1] != 1:
            out.append(np.frombuffer(await a.next_chunk(), np.int16)[0])
        assert out == [800] * 10 + [9000] * 3 + [800] * 10 + [0, 1]
    asyncio.run(run())


class FakeVoiceLive:
    def __init__(self, hub):
        self.hub, self.starts, self.stops, self.is_busy, self.on = hub, 0, 0, False, False
        self.voice_closed = asyncio.Event()
        self.closed = asyncio.Event()
        self.t = time.monotonic()
        self.heard = 0.0
        self.activity = []
        self.fail = 0  # the next N starts fail
        self.body_work = False
        self.spoken = []
        self.send_text = lambda text, source: None  # a brain that takes local-ASR requests

    async def start_voice(self):
        self.starts += 1
        if self.fail:
            self.fail -= 1
            raise ConnectionError("no session")
        self.on, self.t = True, time.monotonic()
        self.voice_closed.clear()

    async def stop_voice(self):
        self.stops += 1
        self.on = False
        self.voice_closed.set()
        self.hub.audio.drop_hold()

    @property
    def voice_on(self):
        return self.on

    def busy(self):
        return self.is_busy

    def last_activity(self):
        return self.t

    def heard_t(self):
        return self.heard

    def speak(self, text):
        if not self.on:
            return False
        self.spoken.append(text)
        return True

    def body_work_active(self):
        return self.body_work

    def _user_activity(self):
        self.activity.append(time.monotonic())


def _voice_hub(monkeypatch, idle=0.3):
    """A Hub in live mode with a fake voice session, fake tonyd and recorded stops / Piper speech."""
    import tonypi.tonyd.client as tc

    monkeypatch.setattr(service, "VOICE_IDLE_S", idle)
    tonyd = {"busy": False}
    monkeypatch.setattr(tc.TonydClient, "__init__", lambda self, *a, **k: None)
    monkeypatch.setattr(tc.TonydClient, "close", lambda self: None)
    monkeypatch.setattr(tc.TonydClient, "state", lambda self: dict(tonyd))
    hub = service.Hub(live_enabled=False)
    hub.mode, hub.spotter = "gpt-live", type("S", (), {"reset": lambda self: None})()
    hub.stopped, hub.said, hub.tonyd = [], [], tonyd
    hub._stop_body = hub.stopped.append

    async def say_local(text):
        hub.said.append(text)
    hub.say_local = say_local
    hub.live = FakeVoiceLive(hub)
    return hub


def test_voice_sleeps_when_idle_and_the_wake_word_brings_it_back(monkeypatch):
    async def run():
        hub = _voice_hub(monkeypatch)
        fake = hub.live
        assert await hub.wake("service start", hold=False) and hub.voice == "on"
        keeper = asyncio.create_task(hub.keep_voice())
        fake.is_busy = True  # a brain turn or a background task: stays awake
        await asyncio.sleep(1.5)
        assert hub.voice == "on" and fake.stops == 0
        fake.is_busy = False
        await asyncio.sleep(1.2)
        assert hub.voice == "off" and fake.stops == 1
        hub.on_keyword("HEY_TONY")
        await asyncio.sleep(0.05)
        assert hub.voice == "on" and fake.starts == 2 and fake.activity  # an autonomy session was told
        assert hub.audio.hold is None or hub.audio.hold_released
        fake.voice_closed.set()  # dropped while working: reconnects
        fake.is_busy = True
        await asyncio.sleep(1.2)
        assert fake.starts == 3 and hub.voice == "on"
        fake.is_busy = False
        await asyncio.sleep(1.2)
        assert hub.voice == "off" and fake.starts == 3
        fake.is_busy = True  # a turn Codex starts by itself after a sleep: no wake for it
        await asyncio.sleep(1.2)
        assert hub.voice == "off" and fake.starts == 3
        keeper.cancel()
    asyncio.run(run())


def test_a_spotted_stop_latches_only_while_the_body_works_and_is_never_replayed(monkeypatch):
    async def run():
        hub = _voice_hub(monkeypatch)
        fake = hub.live
        hub.on_keyword("STOP")  # nothing moves, no body work (a TV said it): ignored, no wake, no replay
        await asyncio.sleep(0.1)
        assert hub.stopped == [] and hub.voice == "off" and fake.starts == 0 and hub.audio.hold is None
        hub.tonyd["busy"] = True  # a move runs: stopped at once (no model), and the voice wakes
        hub.on_keyword("FREEZE")
        await asyncio.sleep(0.1)
        assert hub.stopped == ["freeze"] and hub.voice == "on"
        await hub.sleep_voice("test")
        hub.tonyd["busy"] = False
        fake.body_work = True  # between two trial moves: tonyd idle, but body work is going on
        hub.on_keyword("STOP")
        await asyncio.sleep(0.1)
        assert hub.stopped == ["freeze", "stop"]
    asyncio.run(run())


def test_stop_words_tolerate_fillers_and_punctuation():
    for text in ("Stop.", "Hey, Tony. Stop.", "OK Tony stop", "Whoa, whoa, stop!", "tony, freeze", "No, stop it"):
        assert service.STOP_WORDS.match(text), text
    for text in ("don't stop", "Tony, walk until I say stop", "stopwatch"):
        assert not service.STOP_WORDS.match(text), text


def test_no_unlatch_from_stop_words_or_the_replay_of_a_spotted_stop(monkeypatch):
    import tonypi.tonyd.client as tc

    unlatched = []
    monkeypatch.setattr(tc.TonydClient, "__init__", lambda self, *a, **k: None)
    monkeypatch.setattr(tc.TonydClient, "close", lambda self: None)
    monkeypatch.setattr(tc.TonydClient, "unlatch", lambda self: unlatched.append(1))

    async def run():
        hub = service.Hub(live_enabled=False)
        hub._unlatch("please stop that")  # a stop word anywhere
        hub.spot_stop_t = time.monotonic()
        hub.audio.replay_done_t = time.monotonic()  # the replay of the stop just caught up
        hub._unlatch("Tony, okay")
        await asyncio.sleep(0.05)
        assert unlatched == []
        hub.spot_stop_t = -1e9
        hub._unlatch("walk forward")
        await asyncio.sleep(0.05)
        assert unlatched == [1]
    asyncio.run(run())


def test_a_wake_word_while_a_session_starts_holds_the_mic(monkeypatch):
    async def run():
        hub = _voice_hub(monkeypatch)
        gate = asyncio.Event()
        fake = hub.live

        async def slow_start():
            fake.starts += 1
            await gate.wait()
            fake.on = True
            fake.voice_closed.clear()
        fake.start_voice = slow_start
        hub.audio.ring.append(_chunk(5000))
        t = asyncio.create_task(hub.wake("typed request", hold=False))
        await asyncio.sleep(0.01)
        assert hub.voice == "starting" and hub.audio.hold is None
        hub.on_keyword("TONY")  # "Tony, also wave" while it starts
        assert hub.audio.hold is not None and fake.activity and fake.starts == 1
        gate.set()
        assert await t and hub.audio.hold_released  # replayed into the session
    asyncio.run(run())


def test_speech_waits_for_a_starting_session_and_piper_says_it_if_the_start_fails(monkeypatch, mk):
    async def run():
        hub = _voice_hub(monkeypatch)
        hub.voice = "starting"
        hub.pending_speech.append("Task done.")
        assert await hub.wake("typed request", hold=False)
        await asyncio.sleep(0.05)
        assert hub.live.spoken == ["Task done."] and hub.pending_speech == []
        await hub.sleep_voice("test")
        hub.live.fail = 1
        hub.pending_speech.append("Second task done.")
        assert not await hub.wake("typed request", hold=False)
        assert "Second task done." in hub.said and hub.voice == "off"
    asyncio.run(run())


def test_live_brain_queues_speech_while_the_voice_starts(mk):
    brain = mk()
    del brain.speak
    brain.hub.voice, brain.hub.pending_speech = "starting", []
    assert brain.speak("Hello.") is True and brain.hub.pending_speech == ["Hello."]


def test_failed_reconnects_are_retried_then_the_local_ears_take_over(monkeypatch):
    async def run():
        hub = _voice_hub(monkeypatch)
        fake = hub.live
        assert await hub.wake("service start", hold=False)
        keeper = asyncio.create_task(hub.keep_voice())
        fake.is_busy, fake.fail = True, 1
        fake.voice_closed.set()  # dropped mid-work; the first reconnect fails, the retry (after backoff) works
        await asyncio.sleep(1.3)
        assert hub.voice == "off" and hub.wake_failures == 1
        hub.next_retry_t = 0.0
        await asyncio.sleep(1.2)
        assert hub.voice == "on" and hub.wake_failures == 0
        fake.fail = 2  # GPT-Live is down: after two failed starts the local ears stand in, the brain stays
        fake.voice_closed.set()
        await asyncio.sleep(1.2)
        hub.next_retry_t = 0.0
        await asyncio.sleep(1.2)
        assert hub.mode == "local" and any("own ears" in t for t in hub.said)
        hub.next_retry_t = 0.0  # the retry brings GPT-Live back
        await asyncio.sleep(1.3)
        assert hub.mode == "gpt-live" and hub.voice == "on"
        keeper.cancel()
    asyncio.run(run())


def test_speech_that_leads_nowhere_keeps_the_voice_awake_only_for_a_while(monkeypatch):
    monkeypatch.setattr(service, "HEARD_AWAKE_S", 0.6)

    async def run():
        hub = _voice_hub(monkeypatch)
        fake = hub.live
        assert await hub.wake("service start", hold=False)
        keeper = asyncio.create_task(hub.keep_voice())

        async def tv():
            while True:
                fake.heard = time.monotonic()
                await asyncio.sleep(0.1)
        talk = asyncio.create_task(tv())
        await asyncio.sleep(0.8)
        assert hub.voice == "on"  # heard speech counts ...
        await asyncio.sleep(1.5)
        assert hub.voice == "off"  # ... for HEARD_AWAKE_S past the last real exchange
        talk.cancel()
        keeper.cancel()
    asyncio.run(run())


def test_speaking_counts_as_activity_so_a_result_is_not_cut_off():
    class DC:
        readyState = "open"

        def send(self, raw):
            pass
    hub = type("H", (), {"audio": type("A", (), {"voice_until": 0.0})(), "voice": "on"})()
    live = service.Live(hub)
    live.dc, live.voice_closed = DC(), asyncio.Event()
    assert live.last_activity() == 0.0
    assert live.speak("Background task done.")
    assert time.monotonic() - live.last_activity() < 1
    hub.voice = "off"  # a session going to sleep takes nothing more
    assert not live.speak("Too late.")


def test_a_hold_started_while_the_session_stops_survives(monkeypatch):
    async def run():
        a = service.Audio()
        live = service.Live(type("H", (), {"audio": a, "last_heard_t": 0.0})())
        live.tid, live.proc = "L", type("P", (), {"returncode": None})()
        live.voice_closed, live._codex_session_up = asyncio.Event(), True

        async def rpc(method, params=None, timeout=None):
            a.ring.append(_chunk(7))
            a.start_hold()  # "Tony, ..." heard during thread/realtime/stop
            return {}
        live._rpc = rpc
        a.start_hold()
        a.release_hold()  # the old session's replay, unfinished
        await live.stop_voice()
        assert a.hold is not None and not a.hold_released
    asyncio.run(run())


def test_pending_rpcs_fail_when_the_app_server_exits():
    async def run():
        live = service.Live(type("H", (), {"audio": service.Audio(), "last_heard_t": 0.0})())
        live.proc = type("P", (), {"stdout": _aiter([]), "stdin": type("W", (), {
            "write": lambda self, b: None, "drain": lambda self: _done()})()})()
        fut = asyncio.get_running_loop().create_future()
        live._pending[1] = fut
        await live._reader(asyncio.Queue())
        assert live.closed.is_set() and isinstance(fut.exception(), ConnectionError)
        with pytest.raises(ConnectionError):
            await live._rpc("thread/realtime/start")
    asyncio.run(run())


async def _done():
    return None


async def _aiter(items):
    for x in items:
        yield x


def test_l_answers_are_spoken_without_a_session_and_stale_delegations_are_not_answered(mk):
    brain = mk()
    brain.hub.voice = "off"
    brain.on_notification("turn/started", {"threadId": "L", "turn": {"id": "t0"}})  # Codex's own (no request)
    brain.on_notification("item/completed", {"threadId": "L", "turnId": "t0", "item": {
        "type": "agentMessage", "text": "[FINAL] Noted."}})
    assert brain.spoken == []
    brain.last_typed_t = time.monotonic()  # a typed request's turn: its answer is said
    brain.on_notification("turn/started", {"threadId": "L", "turn": {"id": "t1"}})
    brain.on_notification("item/completed", {"threadId": "L", "turnId": "t1", "item": {
        "type": "agentMessage", "text": "[FINAL] My battery is fine."}})
    brain.on_notification("item/completed", {"threadId": "L", "turnId": "t1", "item": {
        "type": "agentMessage", "text": "[COMMENTARY] Checking."}})
    assert brain.spoken == ["My battery is fine."]
    brain.hub.voice = "on"
    brain.voice_gen = 3
    brain.bridged["t2"] = ("item-old", 2)  # from a session that has gone
    brain.on_notification("item/completed", {"threadId": "L", "turnId": "t2", "item": {
        "type": "agentMessage", "text": "[FINAL] Done."}})
    assert brain.spoken[-1] == "Done."
    brain.on_notification("item/completed", {"threadId": "L", "turnId": "t3", "item": {
        "type": "agentMessage", "text": "[FINAL] Codex says this one itself."}})
    assert brain.spoken[-1] == "Done."  # the session is up and the turn isn't bridged: Codex speaks it


def test_watchdog_reruns_only_the_stuck_turns_own_request(mk):
    brain = mk()

    async def go():
        brain.l_turn, brain.l_turn_started, brain.l_last_item = "t1", time.monotonic() - 40, 0
        brain.last_delegation, brain.last_delegation_t = ("old", "walk forward"), time.monotonic() - 600
        await brain._watch_once()
        assert methods(brain) == ["turn/interrupt"]
        brain.calls.clear()
        brain.l_turn, brain.l_turn_started = "t2", time.monotonic() - 40
        brain.last_delegation, brain.last_delegation_t = ("new", "wave"), brain.l_turn_started + 0.5
        await brain._watch_once()
        assert methods(brain) == ["turn/interrupt", "turn/start"]
    asyncio.run(go())


def test_phone_mic_is_dropped_when_a_wake_word_holds_the_same_words(mk, monkeypatch):
    monkeypatch.setattr(live, "PHONE_WAIT_S", 0.01)
    brain = mk()
    brain.hub.last_wake_t = time.monotonic()
    out = asyncio.run(brain.send_text("wave", "phone_mic"))
    assert "dropped" in out and not brain.calls


def test_brain_is_busy_while_it_works_and_speaks_with_piper_while_asleep(mk):
    brain = mk()
    del brain.speak  # the real one
    said = []

    async def say_local(text):
        said.append(text)
    brain.hub.say_local, brain.hub.voice = say_local, "off"

    async def go():
        assert not brain.busy()
        brain.l_turn = "t1"
        assert brain.busy()
        brain.l_turn = None
        brain.workers["x"] = {"status": "running"}
        assert brain.busy()
        brain.workers["x"]["status"] = "completed"
        assert not brain.busy()
        assert brain.speak("Task done.") is True
        await asyncio.sleep(0.01)
        assert said == ["Task done."]
    asyncio.run(go())


def test_typed_requests_wake_the_voice_without_holding_up_the_turn(mk):
    brain = mk()
    woke = []
    gate = asyncio.Event()

    async def wake(why, hold=True):
        woke.append((why, hold))
        await gate.wait()  # a slow (or failing) start
        return True
    brain.hub.wake = wake
    brain._move_asked = lambda text: None

    async def go():
        await asyncio.wait_for(brain.send_text("what are you doing", "typed"), 1)
        await asyncio.sleep(0)
        assert woke == [("typed request", False)] and methods(brain) == ["turn/start"]
        gate.set()
    asyncio.run(go())


def test_the_speaker_is_reopened_when_aplay_dies(monkeypatch):
    async def run():
        a = service.Audio()
        dead = type("P", (), {"returncode": 1, "stdin": type("W", (), {"is_closing": lambda self: True})()})()
        written = []
        alive = type("P", (), {"returncode": None, "stdin": type("W", (), {
            "is_closing": lambda self: False, "write": lambda self, b: written.append(len(b))})()})()
        a.play = dead

        async def reopen():
            a.play = alive
        a._open_play = reopen
        monkeypatch.setattr(service.asyncio, "sleep", _fast_sleep)
        task = asyncio.create_task(a._mix())
        for _ in range(50):
            await _real_sleep(0)
        task.cancel()
        assert a.play is alive and written
    asyncio.run(run())


def test_speaker_reopens_if_pipe_fails_during_write(monkeypatch):
    async def run():
        a = service.Audio()
        written = []

        class BrokenWriter:
            def is_closing(self):
                return False

            def write(self, data):
                raise BrokenPipeError("speaker disconnected")

        class WorkingWriter:
            def is_closing(self):
                return False

            def write(self, data):
                written.append(len(data))

        a.play = type("P", (), {"returncode": None, "stdin": BrokenWriter()})()
        async def reopen():
            a.play = type("P", (), {"returncode": None, "stdin": WorkingWriter()})()
        a._open_play = reopen
        monkeypatch.setattr(service.asyncio, "sleep", _fast_sleep)
        task = asyncio.create_task(a._mix())
        for _ in range(50):
            await _real_sleep(0)
        assert not task.done()
        assert written
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    asyncio.run(run())


_real_sleep = asyncio.sleep


async def _fast_sleep(s):
    await _real_sleep(0)


def test_a_hold_left_by_a_gone_session_is_dropped_not_replayed_later():
    async def run():
        a = service.Audio()
        a.ring.append(_chunk(111))
        a.start_hold()
        a.release_hold()
        a.hold.extend([_chunk(222)] * 5)  # the session slept before replaying it
        live = service.Live(type("H", (), {"audio": a, "last_heard_t": 0.0})())
        await live.stop_voice()
        assert a.hold is None and not a.replaying
        a.hold = __import__("collections").deque([_chunk(333)])  # stale, released hold (older code path)
        a.hold_released = True
        a.ring.clear()
        a.ring.append(_chunk(444))
        a.start_hold()
        assert [np.frombuffer(b, np.int16)[0] for b in a.hold] == [444] and not a.hold_released
    asyncio.run(run())


def test_voice_stays_awake_while_held_words_are_replayed(monkeypatch):
    monkeypatch.setattr(service, "VOICE_IDLE_S", 0.2)

    async def run():
        hub = service.Hub(live_enabled=False)
        hub.mode, hub.spotter = "gpt-live", type("S", (), {"reset": lambda self: None})()
        hub.live = fake = FakeVoiceLive(hub)
        await hub.wake("test", hold=False)
        hub.audio.start_hold()
        hub.audio.release_hold()
        hub.audio.hold.extend([_chunk(9000)] * 50)
        keeper = asyncio.create_task(hub.keep_voice())
        await asyncio.sleep(1.3)
        assert hub.voice == "on" and fake.stops == 0
        hub.audio.drop_hold()
        await asyncio.sleep(1.2)
        assert hub.voice == "off"
        keeper.cancel()
    asyncio.run(run())


def test_a_mic_track_already_waiting_gets_the_released_hold():
    """The track waits in mic_q.get() when the hold is released (it reads as fast as the mic): it must get it."""
    async def run():
        a = service.Audio()
        a.ring.append(_chunk(5000))
        a.start_hold()
        waiting = asyncio.create_task(a.next_chunk())
        await asyncio.sleep(0.01)
        assert not waiting.done()
        a.hold.append(_chunk(6000))
        a.release_hold()
        first = await asyncio.wait_for(waiting, 1)
        rest = [np.frombuffer(await asyncio.wait_for(a.next_chunk(), 1), np.int16)[0] for _ in range(2)]
        assert np.frombuffer(first, np.int16)[0] == 0 and rest == [5000, 6000]
    asyncio.run(run())


def test_the_replay_starts_after_a_short_real_time_lead_in(monkeypatch):
    monkeypatch.setattr(service, "REPLAY_LEAD_CHUNKS", 3)

    async def run():
        a = service.Audio()
        a.ring.append(_chunk(5000))
        a.start_hold()
        a.release_hold()
        out = [np.frombuffer(await a.next_chunk(), np.int16)[0] for _ in range(4)]
        assert out == [0, 0, 0, 5000]
    asyncio.run(run())


def test_a_repeated_delegation_codex_already_took_is_not_bridged_again(mk):
    brain = mk()
    ensured = []
    brain._ensure_taken = lambda item_id, text, since: ensured.append(item_id) or _done()

    async def go():
        ev = lambda i: {"type": "delegation.created", "item": {"id": i, "content": [
            {"type": "input_text", "text": "Hey Tony, what time is it"}]}}
        brain.on_dc_event(ev("d1"))
        brain.codex_took = time.monotonic()  # Codex took it
        brain.on_dc_event(ev("d2"))  # GPT-Live's copy, 3 s later
        await asyncio.sleep(3.2)
        assert ensured == ["d1"]
    asyncio.run(go())


def test_the_fallback_is_announced_once_however_many_retries_fail(monkeypatch):
    async def run():
        hub = _voice_hub(monkeypatch)
        fake = hub.live
        fake.fail = 10
        keeper = asyncio.create_task(hub.keep_voice())
        for _ in range(2):
            assert not await hub.wake("wake word TONY")
        assert hub.mode == "local" and hub.local_fallback
        for _ in range(2):  # retries from keep_voice fail too
            hub.next_retry_t = 0.0
            await asyncio.sleep(1.2)
        assert sum("own ears" in t for t in hub.said) == 1 and hub.mode == "local"
        fake.fail = 0
        hub.next_retry_t = 0.0
        await asyncio.sleep(1.2)
        assert hub.mode == "gpt-live" and not hub.local_fallback
        keeper.cancel()
    asyncio.run(run())


def test_a_failed_start_tells_a_wake_word_that_joined_it(monkeypatch):
    async def run():
        hub = _voice_hub(monkeypatch)
        fake = hub.live
        gate = asyncio.Event()

        async def failing_start():
            fake.starts += 1
            await gate.wait()
            raise ConnectionError("no session")
        fake.start_voice = failing_start
        t = asyncio.create_task(hub.wake("typed request", hold=False))
        await asyncio.sleep(0.01)
        hub.on_keyword("TONY")  # joins the typed request's start
        gate.set()
        assert not await t
        assert "I can't reach my voice right now." in hub.said
    asyncio.run(run())


def test_a_late_reply_to_a_timed_out_rpc_does_not_kill_the_reader():
    async def run():
        live = service.Live(type("H", (), {"audio": service.Audio(), "last_heard_t": 0.0})())
        lines = []
        live.proc = type("P", (), {"stdin": type("W", (), {"write": lambda self, b: lines.append(b),
                                                           "drain": lambda self: _done()})()})()
        with pytest.raises(asyncio.TimeoutError):
            await live._rpc("thread/realtime/start", timeout=0.01)
        assert not live._pending
        await live._dispatch({"id": 1, "result": {}}, asyncio.Queue())  # the late reply: ignored
        q = asyncio.Queue()
        await live._dispatch({"method": "thread/realtime/sdp", "params": {"sdp": "x"}}, q)
        assert q.get_nowait() == "x"
    asyncio.run(run())


def test_a_cancelled_start_leaves_no_sdp_waiter_behind():
    """wake's timeout cancels start_voice while it waits for the SDP answer: the next start must get its own."""
    async def run():
        q: asyncio.Queue = asyncio.Queue()
        closed = asyncio.Event()

        async def unless_closed(aw):  # the same shape as start_voice's helper
            task, stop = asyncio.ensure_future(aw), asyncio.ensure_future(closed.wait())
            try:
                await asyncio.wait({task, stop}, return_when=asyncio.FIRST_COMPLETED)
                return task.result()
            finally:
                stop.cancel()
                if not task.done():
                    task.cancel()
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(unless_closed(q.get()), 0.05)
        await asyncio.sleep(0)
        q.put_nowait("answer-2")
        assert await asyncio.wait_for(q.get(), 0.5) == "answer-2"
    asyncio.run(run())


def test_late_closed_of_a_stopped_session_does_not_close_the_next(caplog):
    async def run():
        live = service.Live(type("H", (), {"audio": service.Audio(), "last_heard_t": 0.0})())
        live._stops_pending = 1  # a stop was sent; its closed (requested) comes late, after the new start
        live.voice_closed = asyncio.Event()
        await live._dispatch({"method": "thread/realtime/closed", "params": {"reason": "requested"}}, asyncio.Queue())
        assert not live.voice_closed.is_set() and live._stops_pending == 0
        await live._dispatch({"method": "thread/realtime/closed", "params": {"reason": "transport_closed"}},
                             asyncio.Queue())
        assert live.voice_closed.is_set()
    asyncio.run(run())


def test_a_stuck_turn_from_an_older_session_is_rerun_and_its_answer_spoken(mk):
    brain = mk()

    async def go():
        brain.voice_gen = 6  # the link dropped and came back
        brain.l_turn, brain.l_turn_started, brain.l_last_item = "t1", time.monotonic() - 40, 0
        brain.last_delegation, brain.last_delegation_t = ("d5", "walk to the door"), brain.l_turn_started + 0.5
        brain.last_delegation_gen = 5
        await brain._watch_once()
        assert methods(brain) == ["turn/interrupt", "turn/start"]
        turn = next(iter(brain.bridged))
        assert brain.bridged[turn] == ("d5", 5)
        brain.on_notification("item/completed", {"threadId": "L", "turnId": turn, "item": {
            "type": "agentMessage", "text": "[FINAL] I'm at the door."}})
        assert brain.spoken == ["I'm at the door."]  # spoken as Tony, not into the dead delegation
    asyncio.run(go())


def test_a_typed_request_steering_a_running_turn_gets_its_answer_spoken(mk):
    brain = mk()
    brain.hub.voice = "off"
    brain._move_asked = lambda text: None
    brain.on_notification("turn/started", {"threadId": "L", "turn": {"id": "t0"}})  # Codex's own turn
    assert not brain.l_turn_owner
    asyncio.run(brain.send_text("what time is it", "local_asr"))
    assert methods(brain)[-1] == "turn/steer" and brain.l_turn_owner
    brain.on_notification("item/completed", {"threadId": "L", "turnId": "t0", "item": {
        "type": "agentMessage", "text": "[FINAL] It's noon."}})
    assert brain.spoken == ["It's noon."]


def test_the_replay_of_a_spotted_stop_does_not_stop_again(monkeypatch):
    async def run():
        hub = service.Hub(live_enabled=False)
        stops = []
        hub._stop_body = stops.append
        hub.spot_stop_t = time.monotonic()
        hub.audio.hold, hub.audio.hold_released = __import__("collections").deque(), True  # replaying
        hub.on_transcript("Stop.")
        hub.audio.drop_hold()
        hub.utt_parts = []
        hub.on_transcript("Stop!")  # said live after the replay: stops
        assert stops == ["Stop!"]
        hub.utt_timer.cancel()
    asyncio.run(run())
