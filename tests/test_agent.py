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
