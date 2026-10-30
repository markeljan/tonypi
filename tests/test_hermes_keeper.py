"""BodyKeeper: settle after body moves, get up after a fall, all without an LLM (fake tonyd, simulated time)."""

from tonypi.hermes.keeper import SETTLE_AFTER_S, STOP_GRACE_S, BodyKeeper


class FakeTonyd:
    def __init__(self) -> None:
        self.st = {"lying": False, "asleep": False, "tilt_deg": 2.0}
        self.runs: list[str] = []
        self.stops = 0

    def state(self, fresh: bool = False) -> dict:
        return dict(self.st)

    def run(self, skill, count=1, wait=True):
        self.runs.append(skill)
        return {"state": "queued"}

    def stop(self) -> None:
        self.stops += 1


def make():
    t = [100.0]
    body, said, notes = FakeTonyd(), [], []
    k = BodyKeeper(body, said.append, on_fall=notes.append, clock=lambda: t[0])
    return k, body, said, notes, t


def job(k, skill, state, **kw):
    k.on_job({"event": "job", "skill": skill, "state": state, **kw})


def test_settles_after_a_walk_unless_the_next_move_comes_first():
    k, body, said, notes, t = make()
    job(k, "walk_forward", "running"), job(k, "walk_forward", "done")
    t[0] += SETTLE_AFTER_S / 2
    assert k.tick() is None
    job(k, "turn_left", "running")  # the brain's next step: no settle in between
    t[0] += SETTLE_AFTER_S
    assert k.tick() is None
    job(k, "turn_left", "done")
    t[0] += SETTLE_AFTER_S + 0.1
    assert k.tick() == "settle" and body.runs == ["settle"]
    t[0] += 5
    assert k.tick() is None  # once


def test_no_settle_after_head_moves_stops_or_holds():
    k, body, said, notes, t = make()
    for skill, state, kw in (("look_left", "done", {}), ("walk_forward", "cancelled", {}),
                             ("_play", "done", {"hold_s": 10})):
        job(k, skill, "running"), job(k, skill, state, **kw)
        t[0] += 3
        assert k.tick() is None
    job(k, "walk_forward", "running"), job(k, "walk_forward", "done")
    k.note_stop()
    t[0] += 3
    assert k.tick() is None and body.runs == []


def test_gets_up_after_a_fall_and_tells_the_brain():
    k, body, said, notes, t = make()
    job(k, "turn_left", "running"), job(k, "turn_left", "done")
    body.st.update(lying=True, tilt_deg=91)
    assert k.tick() == "get_up"
    assert body.runs == ["get_up"] and body.stops == 1 and said and notes
    job(k, "get_up", "running")
    t[0] += 1
    assert k.tick() is None  # getting up
    body.st.update(lying=False, tilt_deg=3)
    job(k, "get_up", "done")
    t[0] += 3
    assert k.tick() is None  # no settle after get_up


def test_lying_on_purpose_is_left_alone():
    k, body, said, notes, t = make()
    job(k, "lie_down", "running"), job(k, "lie_down", "done")
    body.st.update(lying=True, tilt_deg=88)
    t[0] += 3
    assert k.tick() is None and body.runs == []
    job(k, "get_up", "running"), job(k, "get_up", "done")
    body.st.update(lying=False)
    job(k, "walk_forward", "running"), job(k, "walk_forward", "done")
    body.st.update(lying=True)  # now it's a fall
    assert k.tick() == "get_up"


def test_gives_up_after_two_tries_and_waits_after_a_stop():
    k, body, said, notes, t = make()
    body.st.update(lying=True)
    k.note_stop()
    assert k.tick() is None  # someone may be holding him
    t[0] += STOP_GRACE_S + 1
    assert k.tick() == "get_up"
    job(k, "get_up", "running"), job(k, "get_up", "done")
    t[0] += 1
    assert k.tick() == "get_up"
    job(k, "get_up", "running"), job(k, "get_up", "done")
    t[0] += 1
    assert k.tick() == "help" and "help" in said[-1]
    t[0] += 1
    assert k.tick() is None and body.runs == ["get_up", "get_up"]
