"""BodyKeeper regression tests with simulated time and fake hardware."""
from tonypi.hermes.keeper import SETTLE_AFTER_S, BodyKeeper

class FakeTonyd:
    def __init__(self):
        self.st = {"lying": False, "asleep": False, "tilt_deg": 2.0}
        self.runs = []
        self.stops = 0
    def state(self, fresh=False):
        return dict(self.st)
    def run(self, skill, count=1, wait=True):
        self.runs.append(skill)
        return {"state": "queued"}
    def stop(self):
        self.stops += 1

def make():
    t = [100.0]
    body, said, notes = FakeTonyd(), [], []
    k = BodyKeeper(body, said.append, on_fall=notes.append, clock=lambda: t[0])
    return k, body, said, notes, t

def job(k, skill, state, **kw):
    k.on_job({"skill": skill, "state": state, **kw})

def test_settle_and_new_job():
    k, body, _, _, t = make()
    job(k, "walk_forward", "running")
    job(k, "walk_forward", "done")
    t[0] += SETTLE_AFTER_S / 2
    assert k.tick() is None
    job(k, "turn_left", "running")
    t[0] += 3
    assert k.tick() is None
    job(k, "turn_left", "done")
    t[0] += 3
    assert k.tick() == "settle"
    assert body.runs == ["settle"]
    assert k.tick() is None

def test_no_settle_after_head_stop_cancel_or_hold():
    k, body, _, _, t = make()
    for skill, state, kw in (("look_left", "done", {}), ("walk_forward", "cancelled", {}),
                             ("_play", "done", {"hold_s": 60})):
        job(k, skill, "running")
        job(k, skill, state, **kw)
        t[0] += 3
        assert k.tick() is None
    job(k, "walk_forward", "done")
    k.note_stop()
    t[0] += 3
    assert k.tick() is None
    assert body.runs == []

def test_no_automatic_recovery_even_after_repeated_fall_checks():
    k, body, said, notes, t = make()
    job(k, "walk_forward", "done")
    body.st.update(lying=True, tilt_deg=91)
    for _ in range(10):
        t[0] += 30
        assert k.tick() is None
    assert body.runs == [] and body.stops == 0
    assert said == [] and notes == [] and k.events == []
    body.st.update(lying=False)
    assert k.tick() is None

def test_intentional_lying_and_manual_get_up():
    k, body, _, _, t = make()
    job(k, "lie_down", "running")
    job(k, "lie_down", "done")
    body.st.update(lying=True)
    t[0] += 3
    assert k.tick() is None
    job(k, "get_up", "running")
    body.st.update(lying=False)
    job(k, "get_up", "done")
    t[0] += 3
    assert k.tick() is None
    assert body.runs == []
