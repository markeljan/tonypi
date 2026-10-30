"""Voice service + live brain without audio, Codex, tonyd or the brain store (fakes only)."""
import asyncio
import json
import os
import re
import time

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
