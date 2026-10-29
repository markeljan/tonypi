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
    from tonypi.agent.body import Body
    b = Body.__new__(Body)
    b.board, b.cancelled, b._abort_reason, b._last_dance, b.asleep = _FakeBoard(lying), threading.Event(), None, None, asleep
    b.player = _FakePlayer(b.board)
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
    assert body.player.played == ["stand_up_back", "stand_slow", "wave"]


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
