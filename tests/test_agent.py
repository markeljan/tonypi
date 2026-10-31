from tonypi.agent.brain import STOP_RE, Plan, postprocess
from tonypi.agent.speech import command_from_words


def test_wake_phrase_removed():
    w = lambda s, t0=0.0: [(x, t0 + 0.3 * i) for i, x in enumerate(s.split())]
    # real case: "hey tony" heard as "eight only", spotter fired 1 s after it
    assert command_from_words(w("EIGHT ONLY NOD YOUR HEAD"), 1.4) == "nod your head"
    assert command_from_words(w("WE ATE LUNCH HAY TONY LOOK TO THE LEFT"), 2.0) == "look to the left"
    assert command_from_words(w("HAYTONY DANCE"), 0.5) == "dance"
    assert command_from_words(w("ATHONY NOD YOUR HEAD"), 0.5) == "nod your head"
    assert command_from_words(w("ATHONY"), 0.5) == ""  # paused after the wake phrase: listen again
    assert command_from_words(w("HEY DANCE FOR US"), 0.5) == "dance for us"  # "tony" not transcribed at all
    assert command_from_words(w("DANCE FOR US"), None) == "dance for us"  # second segment after a pause


def test_stop_rule():
    assert STOP_RE.match("stop")
    assert STOP_RE.match("Stop it now!")
    assert not STOP_RE.match("don't stop dancing")


def test_postprocess_dedupes_and_validates():
    p = postprocess("wave at us", Plan([("wave", 1), ("wave", 1), ("fly", 1), ("bow", 9)], "*Hi*"))
    assert p.actions == [("wave", 1), ("bow", 5)]
    assert p.say == "Hi"


def test_postprocess_adds_named_moves_and_vision():
    assert ("dance", 1) in postprocess("danced for us", Plan()).actions
    for heard in ("show me some confoo", "show me some come foo", "show me some can foo", "kung fu"):
        assert ("kung_fu", 1) in postprocess(heard, Plan()).actions
    p = postprocess("what do you see", Plan([("look_left", 1), ("look_right", 1)], "Let me look."))
    assert p.actions == [("describe_view", 1)]


def test_status_questions_use_real_sensors():
    p = postprocess("how's your battery?", Plan([], "My battery is full."))
    assert p.actions == [("report_status", 1)] and p.say == ""


def test_sleep_and_wake_words():
    assert ("sleep", 1) in postprocess("go to sleep", Plan()).actions
    assert ("sleep", 1) in postprocess("power down for a while", Plan()).actions
    assert ("get_up", 1) in postprocess("wake up", Plan()).actions
    assert ("lie_down", 1) in postprocess("lie down", Plan()).actions


class _Imu:
    def __init__(self, accel):
        self.accel = accel


class _FakeBoard:
    def __init__(self, lying):
        self.imu = _Imu((0.0, 0.0, -0.97) if lying else (-0.97, 0.0, 0.0))
        self.battery_mv, self.calls = 11500, []

    def head(self, ms, pulses): self.calls.append(("head", pulses))
    def torque(self, ids, on): self.calls.append(("torque", on))
    def rgb(self, pixels): self.calls.append(("rgb",))
    def bus_read(self, sid, cmd): return 40


class _FakePlayer:
    def __init__(self, board): self.board, self.played = board, []
    def play(self, motion, speed=1.0):
        self.played.append(motion.name)
        if motion.name.startswith("stand_up") or motion.name == "stand_slow":
            self.board.imu = _Imu((-0.97, 0.0, 0.0))  # now upright
        if motion.name == "lie_down":
            self.board.imu = _Imu((0.0, 0.0, -0.97))
        return True
    def stop(self): pass


def _body(lying, asleep=False):
    import threading
    from tonypi.tonyd.body import Body
    b = Body.__new__(Body)
    b.board, b.cancelled, b._abort_reason, b._last_dance, b.asleep = _FakeBoard(lying), threading.Event(), None, None, asleep
    b.player = _FakePlayer(b.board)
    import tempfile
    from pathlib import Path
    from tonypi.tonyd.body import DEFAULT_SKILLS
    from tonypi.tonyd.studio import Studio
    b.studio = Studio(Path(tempfile.mkdtemp()), default_skills=DEFAULT_SKILLS)
    # Routing tests need named synthetic data, never the owner's factory recordings.
    from tonypi.motion.library import Motion, Frame
    b.studio.load_motion = lambda name: Motion(name, [Frame(20, {1: 510})])
    return b


def test_every_action_routes_without_error():
    from tonypi.agent.brain import ACTIONS
    for name in ACTIONS:
        if name in ("describe_view", "report_status", "stop"):
            continue  # handled by the daemon, not Body.run
        for lying, asleep in ((False, False), (True, False), (True, True)):
            body = _body(lying, asleep)
            body._sleep = lambda s: True
            assert body.run(name, 2, lambda s: None) in (True, False), name


def test_sleep_and_wake():
    body = _body(lying=True)
    assert body.run("sleep", 1, lambda s: None) and body.asleep
    assert ("torque", False) in body.board.calls
    assert body.run("wave", 1, lambda s: None) and not body.asleep
    assert body.player.played == ["wave"]  # tonyd doesn't get up by itself: falls and sleep are the brain's call


def test_voice_agent_gets_up_before_moving_when_lying():
    """The voice agent is one such brain: BodyLink gets up first when Tony is lying and the skill moves the body."""
    from tonypi.agent.body_link import BodyLink

    class Tonyd:
        def __init__(self, lying):
            self.lying, self.ran = lying, []

        def state(self, fresh=False):
            return {"lying": self.lying}

        def run(self, skill, count=1):
            self.ran.append(skill)
            if skill == "get_up":
                self.lying = False
            return {"state": "done", "said": []}

    for lying, skill, expected in ((True, "wave", ["get_up", "wave"]), (False, "wave", ["wave"]),
                                   (True, "look_left", ["look_left"])):
        link = BodyLink.__new__(BodyLink)
        link.cancelled, link.tonyd = __import__("threading").Event(), Tonyd(lying)
        assert link.run(skill, 1, lambda s: None)
        assert link.tonyd.ran == expected


def test_sleep_relaxes_even_if_lying_down_fails():
    body = _body(lying=False)
    body.player.play = lambda motion, speed=1.0: body.cancelled.set()  # "stop" during lie_down
    assert not body.run("sleep", 1, lambda s: None) and not body.asleep
    assert ("torque", False) in body.board.calls


def test_thermal_stop_relaxes_instead_of_standing():
    body = _body(lying=False)
    body._watch = lambda ids, guard_fall, done: setattr(body, "_abort_reason", "hot")
    said = []
    assert not body.run("wave", 1, said.append)
    assert body.board.calls[-1] == ("torque", False)
    assert body.player.played == [] and "motors off" in said[0]


def test_stand_while_lying_gets_up_first():
    body = _body(lying=True)
    assert body.run("stand", 1, lambda s: None)
    assert body.player.played == ["stand_up_back", "stand_slow"]
    body.player.played.clear()
    assert body.run("stand", 1, lambda s: None) and body.player.played == ["stand_slow"]  # already upright


def test_lfm_second_pass(monkeypatch):
    import io, json
    import numpy as np
    from tonypi.agent import speech

    def fake_urlopen(reply):
        class R(io.BytesIO):
            def __enter__(self): return self
            def __exit__(self, *a): pass
        return lambda req, timeout=0: R(json.dumps({"choices": [{"message": {"content": reply}}]}).encode())

    audio = np.zeros(16000, np.float32)
    monkeypatch.setattr(speech.urllib.request, "urlopen", fake_urlopen("We ate. Hey Tony, show me some kung fu."))
    assert speech.lfm_transcribe(audio, "show me some confoo", True) == "show me some kung fu"
    monkeypatch.setattr(speech.urllib.request, "urlopen", fake_urlopen("Dance for us."))
    assert speech.lfm_transcribe(audio, "dense for us", False) == "Dance for us"   # second segment: no wake phrase
    assert speech.lfm_transcribe(audio, "dense for us", True) == "dense for us"    # wake phrase missing: fallback
    monkeypatch.setattr(speech.urllib.request, "urlopen", fake_urlopen("Hey Tony, dance for us."))
    assert speech.lfm_transcribe(audio, "dance for us", False) == "dance for us"   # wake phrase in the follow-up window

    def down(req, timeout=0):
        raise OSError("connection refused")
    monkeypatch.setattr(speech.urllib.request, "urlopen", down)
    assert speech.lfm_transcribe(audio, "wave", True) == "wave"


def _sse(text):
    import io, json
    chunks = [text[i:i + 7] for i in range(0, len(text), 7)]
    lines = [("data: " + json.dumps({"choices": [{"delta": {"content": c}}]}) + "\n").encode() for c in chunks]

    class R(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *a): pass
    return lambda req, timeout=0: R(b"".join(lines) + b"data: [DONE]\n")


def test_plan_stream_hands_out_moves_early(monkeypatch):
    from tonypi.agent import brain
    raw = '{"actions":[{"action":"kung_fu","count":1},{"action":"kung_fu","count":1},{"action":"bow","count":1}],"say":"Hi"}'
    monkeypatch.setattr(brain.urllib.request, "urlopen", _sse(raw))
    got = []
    p = brain.Brain().plan_stream("show me kung fu then bow", on_action=lambda n, c: got.append(n))
    assert got == ["kung_fu", "bow"]                       # repeat dropped, both handed out while streaming
    assert p.actions == [("kung_fu", 1), ("bow", 1)] and p.early == 2 and p.say == "Hi"


def test_plan_stream_vision_rule_overrides_looks(monkeypatch):
    from tonypi.agent import brain
    monkeypatch.setattr(brain.urllib.request, "urlopen", _sse('{"actions":[{"action":"look_left","count":1}],"say":"x"}'))
    got = []
    p = brain.Brain().plan_stream("what do you see?", on_action=lambda n, c: got.append(n))
    assert got == [] and p.actions == [("describe_view", 1)]


def test_execute_runs_moves_one_at_a_time_in_order():
    import queue, threading, time
    from tonypi.agent.brain import Plan
    from tonypi.agent.daemon import Agent

    log, running = [], []

    class Body:
        cancelled = threading.Event()
        def run(self, name, count, say):
            assert not running, "two moves at once"
            running.append(name); time.sleep(0.05); running.pop(); log.append(name)
            return True
        def settle(self): log.append("settle")

    class Brain:
        def plan_stream(self, text, audio, on_action):
            on_action("wave", 1); time.sleep(0.02); on_action("bow", 1)
            p = Plan([("wave", 1), ("bow", 1), ("nod", 1)], "hello"); p.early = 2
            return p

    class Speaker:
        def say(self, s): log.append("say:" + s)

    a = Agent.__new__(Agent)
    a.body, a.brain, a.speaker, a.emit = Body(), Brain(), Speaker(), lambda *x, **k: None
    a._execute(1, "wave then bow")
    assert [x for x in log if not x.startswith("say")] == ["wave", "bow", "nod", "settle"]
    assert "say:hello" in log


def test_agent_ignores_commands_while_the_robot_lock_is_held(tmp_path, monkeypatch):
    import fcntl
    from tonypi.agent import daemon

    lock = tmp_path / "robot.lock"
    monkeypatch.setattr(daemon, "ROBOT_LOCK", str(lock))
    assert not daemon.robot_reserved()
    with open(lock, "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        assert daemon.robot_reserved()
    assert not daemon.robot_reserved()


def test_switch_names_map_to_live_or_local_only():
    from tonypi.switch import parse_switch

    assert parse_switch("Tony, switch to local") == "local"
    assert parse_switch("switch back to default") == "local"
    assert parse_switch("which to alive") == "live"  # ASR mangles both words
    assert parse_switch("switch to our own") == "live"  # retired harness names mean live now
    assert parse_switch("use the codex brain") == "live"
    assert parse_switch("switch to Hermes") is None and parse_switch("switch to pi") is None
    assert parse_switch("walk to the chair") is None and parse_switch("switch off the light") is None


def test_agent_switch_to_live_starts_the_voice_and_local_only_acks(monkeypatch):
    import itertools
    from tonypi import switch
    from tonypi.agent import daemon

    started, said = [], []
    monkeypatch.setattr(switch.subprocess, "Popen", lambda cmd: started.append(cmd))
    monkeypatch.setattr(daemon, "request_switch", switch.request_switch)
    a = daemon.Agent.__new__(daemon.Agent)
    a.ids, a.debug, a.emit = itertools.count(1), False, lambda *x, **k: None
    a.speaker = type("S", (), {"say": lambda self, s: said.append(s)})()
    a.submit("switch to local", "text")
    a.submit("switch to live", "text")
    assert started == [["sudo", "systemctl", "start", "--no-block", "tonypi-voice.service"]]
    assert said == ["I'm already the local agent.", "Switching to live mode."]
