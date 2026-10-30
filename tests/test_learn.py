"""Body learning (tonypi.learn) against a fake tonyd and a fake phone: no robot, no bridge."""

import fcntl
import json
import os
import time

import pytest

from tonypi.learn import gait, metrics
from tonypi.learn.phone import episode_summary
from tonypi.learn.runner import Lab, LabError
from tonypi.motion.library import DATA_DIR
from tonypi.tonyd.client import TonydError

TURN = {"frames": [{"ms": 200, "pos": {"4": 650, "12": 350, "17": 500}}, {"ms": 200, "pos": {"4": 600, "12": 400}}]}


class FakeTonyd:
    """Enough of TonydClient for the runner. `falls` scripts which moves end lying; yaw comes from `yaws`."""

    def __init__(self):
        self.lying, self.latched, self.busy, self.battery, self.hot = False, False, False, 11800, 45
        self.falls, self.yaws, self.get_up_works, self.cancel = [], {}, True, False
        self.ran, self.jid, self.pos, self.blocked = [], 0, {i: 500 for i in range(1, 17)}, {}

    def call(self, op, timeout=10, **kw):
        if op == "skills":
            return {"details": {k: {"motions": [f"*{k}"]} for k in ("turn_left", "walk_forward", "wave")}}
        if op == "motion_get":
            return {"name": kw["name"], **json.loads(json.dumps(TURN))}
        raise AssertionError(op)

    def state(self, fresh=False):
        return {"busy": self.busy, "lying": self.lying, "tilt_deg": 88.0 if self.lying else 3.0,
                "battery_mv": self.battery, "hottest": [8, "left shoulder", self.hot], "temps": {"8": self.hot},
                "stop_latched": self.latched}

    def _job(self, skill, count, kw, frames=None):
        if self.latched:
            raise TonydError("stopped by the user (stop latch)")
        self.jid += 1
        self.ran.append((skill, count, kw))
        state, fell = "done", False
        if skill == "get_up":
            self.lying = not self.get_up_works
        elif skill == "lie_down":
            self.lying = True
        elif skill not in ("settle", "stand") and not (kw.get("label") or "").startswith("sweep"):
            fell = self.falls.pop(0) if self.falls else False
            self.lying = self.lying or fell
            if self.cancel:
                state = "cancelled"
        yaw = self.yaws.get(skill, [0.0])
        y = yaw.pop(0) if len(yaw) > 1 else yaw[0]
        return {"id": self.jid, "skill": skill, "count": count, "state": state, "seconds": 2.0, "said": [],
                "max_tilt_deg": 55.0 if fell else 12.0, "yaw_deg": y * count, "label": kw.get("label")}

    def run(self, skill, count=1, wait=True, timeout=180, **kw):
        return self._job(skill, count, kw)

    def play(self, frames, hold_s=0, wait=True, timeout=180, **kw):
        for k, v in frames[-1]["pos"].items():  # a sweep ramp: the servo gets there unless blocked
            lo, hi = self.blocked.get(int(k), (0, 1000))
            self.pos[int(k)] = max(lo, min(hi, v))
        return self._job("_play", 1, kw, frames)

    def joints(self):
        return {"raw": dict(self.pos), "pos": {str(k): v for k, v in self.pos.items()}}


class FakePhone:
    def __init__(self, mode="companion", moved=0.2):
        self._mode, self.moved, self.labels, self.tracked = mode, moved, [], 0

    def mode(self):
        return self._mode

    def track(self, target="the small humanoid robot"):
        self.tracked += 1
        return {"tracking": True}

    def record_start(self, label, max_s=60):
        self.labels.append(label)
        return {"recording": label}

    def record_stop(self):
        return {"id": f"ep-{len(self.labels)}", "path": "/nowhere"}

    def episode(self, episode_id):
        return {"id": episode_id, "frames": 20, "frames_tracked": 20, "target_moved_m": self.moved}


@pytest.fixture
def lab(tmp_path):
    return Lab(tonyd=FakeTonyd(), phone=FakePhone(), root=tmp_path / "learning", lock_path=str(tmp_path / "lock"),
               sleep=lambda s: None)


def _ledger(lab):
    return [json.loads(x) for x in lab.ledger_path.read_text().splitlines()]


# ---- gait ----------------------------------------------------------------------------------------------------------

def test_gait_transforms_are_pure_and_clamped():
    st = gait.stand_pose()
    assert st[4] == 600 and st[15] == 200 and len(st) == 16
    assert gait.time_scale(TURN, 0.5)["frames"][0] == {"ms": 100, "pos": {"12": 350, "4": 650}}  # 17 dropped
    assert gait.time_scale(TURN, 0.01)["frames"][0]["ms"] == gait.MIN_MS
    big = gait.amplitude(TURN, 2.0)["frames"][0]["pos"]
    assert big == {"4": 700, "12": 300}  # twice as far from the stand pose (600 / 400)
    assert gait.amplitude(TURN, 2.0, servos=[4])["frames"][0]["pos"] == {"4": 700, "12": 350}
    assert gait.offset(TURN, {"4": 500})["frames"][0]["pos"]["4"] == 1000
    assert len(gait.concat(TURN, TURN)["frames"]) == 4 and len(gait.repeat(TURN, 3)["frames"]) == 6
    assert TURN["frames"][0]["pos"]["4"] == 650  # inputs untouched
    m = gait.apply(TURN, {"time_scale": 2, "amplitude": 1.5, "offset": {"12": 10}, "repeat": 2})
    assert len(m["frames"]) == 4 and m["frames"][0] == {"ms": 400, "pos": {"12": 335, "4": 675}}


def test_mirror_turns_turn_left_into_turn_right():
    left, right = (json.loads((DATA_DIR / f"{n}.json").read_text()) for n in ("turn_left", "turn_right"))
    assert gait.mirror(left)["frames"] == gait.apply(right, None)["frames"]
    assert gait.mirror(gait.mirror(left))["frames"] == gait.apply(left, None)["frames"]


def test_digest_matches_tonyds_job_hash(tmp_path, monkeypatch):
    from tonypi.tonyd import server

    class Studio:
        user_dir = tmp_path

    class Body:
        studio = Studio()

        def cancel(self):
            pass

    monkeypatch.setattr(server, "JOBS_LOG", tmp_path / "jobs.jsonl")
    job = server.Tonyd(Body()).submit("", frames=TURN["frames"])
    assert job.params["hash"] == gait.digest(TURN)


# ---- metrics -------------------------------------------------------------------------------------------------------

def test_scores_by_objective():
    job = {"state": "done", "seconds": 4.0, "max_tilt_deg": 20.0, "yaw_deg": 5.0}
    up, down = {"tilt_deg": 3.0, "lying": False}, {"tilt_deg": 85.0, "lying": True}
    w = metrics.score("walk", job, {"temps": {"3": 40}}, {**up, "temps": {"3": 43}}, {"frames_tracked": 9,
                                                                                    "target_moved_m": 0.4}, 4)
    assert w["basis"] == "phone" and w["forward_mps"] == 0.1 and w["score"] == pytest.approx(0.1 - 0.1)
    assert w["dT_max"] == 3 and not w["fell"]
    no_phone = metrics.score("walk", job, None, up, None, 4)
    assert no_phone["basis"] == "steps" and no_phone["forward_mps"] == pytest.approx(0.05)
    assert metrics.score("walk", job, None, down, None, 4)["score"] < -1.9  # a fall costs 2
    assert metrics.score("walk", job, None, {"tilt_deg": 50.0}, None, 4)["fell"]  # > 45 deg is a fall
    t = metrics.score("turn", {**job, "yaw_deg": 30.0, "max_tilt_deg": 25.0}, None, up, None, 3)
    assert t["yaw_per_step"] == 10 and t["score"] == pytest.approx(10 - 0.5)
    b = metrics.score("balance", {**job, "state": "failed"}, None, up)
    assert b["held_s"] == 0.0 and metrics.score("balance", job, None, up)["held_s"] == 4.0
    assert metrics.score("pose", job, None, up)["score"] == pytest.approx(-0.3)
    with pytest.raises(ValueError):
        metrics.score("fly", job)
    assert metrics.stats([1.0, 2.0, 3.0]) == {"n": 3, "mean": 2.0, "std": 1.0}


def test_episode_summary_reads_the_tracked_path(tmp_path):
    lines = [{"seq": i, "rx": 100 + i * 0.2, "tracked": {"world": [0.1 * i, 0.5, 0.0], "lost": i == 2}}
             for i in range(5)]
    (tmp_path / "frames.jsonl").write_text("\n".join(json.dumps(x) for x in lines) + "\n{torn")
    ep = episode_summary(tmp_path)
    assert ep["frames"] == 5 and ep["frames_tracked"] == 4
    assert ep["target_moved_m"] == 0.4 and ep["path_m"] == 0.4 and ep["path"][-1] == [0.8, 0.4, 0.0]
    assert episode_summary(tmp_path / "missing")["error"]


def test_recovery_goal_eligibility_preserves_ranking_and_legacy_recovered():
    job = {"state": "done", "seconds": 6.33, "end_tilt_deg": 4.0}
    before, after = {"lying": True}, {"lying": False, "tilt_deg": 3.0}
    result = metrics.score("recover", job, before, after)
    assert result["recovery_goal_eligible"] and result["recovered"]
    assert result["score"] == pytest.approx(-0.6165)
    # Completed upright poses remain useful for ranking, but cannot prove a recovery from lying.
    standing = metrics.score("recover", job, {"lying": False}, after)
    assert standing["recovered"] and standing["score"] == result["score"]
    assert not standing["recovery_goal_eligible"]
    assert metrics.score("recover", job, before, {"lying": False})["recovery_goal_eligible"]
    assert not metrics.score("recover", {"state": "done"}, before, after)["recovery_goal_eligible"]


@pytest.mark.parametrize("job_changes,before,after", [
    ({}, None, {"lying": False, "tilt_deg": 3.0}),
    ({}, {}, {"lying": False, "tilt_deg": 3.0}),
    ({}, {"lying": True}, None),
    ({}, {"lying": True}, {"tilt_deg": 3.0}),
    ({}, {"lying": True}, {"lying": True, "tilt_deg": 3.0}),
    ({"state": "cancelled"}, {"lying": True}, {"lying": False, "tilt_deg": 3.0}),
    ({"seconds": None}, {"lying": True}, {"lying": False, "tilt_deg": 3.0}),
    ({"seconds": float("nan")}, {"lying": True}, {"lying": False, "tilt_deg": 3.0}),
    ({"seconds": -1.0}, {"lying": True}, {"lying": False, "tilt_deg": 3.0}),
    ({}, {"lying": True}, {"lying": False}),
    ({}, {"lying": True}, {"lying": False, "tilt_deg": None}),
    ({}, {"lying": True}, {"lying": False, "tilt_deg": 10.0}),
    ({}, {"lying": True}, {"lying": False, "tilt_deg": -1.0}),
    ({}, {"lying": True}, {"lying": False, "tilt_deg": float("nan")}),
    ({"lying_after": True}, {"lying": True}, {"lying": False, "tilt_deg": 3.0}),
])
def test_recovery_goal_eligibility_requires_complete_measured_evidence(job_changes, before, after):
    job = {"state": "done", "seconds": 6.33, **job_changes}
    assert not metrics.score("recover", job, before, after)["recovery_goal_eligible"]


def test_recovery_goal_tilt_threshold_is_strict():
    job = {"state": "done", "seconds": 6.33}
    assert metrics.score("recover", job, {"lying": True},
                         {"lying": False, "tilt_deg": 9.999})["recovery_goal_eligible"]


# ---- runner --------------------------------------------------------------------------------------------------------

def test_batch_runs_guarded_labelled_trials_and_records_them(lab):
    lab.tonyd.yaws["turn_left"] = [9.0, 11.0, 10.0]
    r = lab.batch({"skill": "turn_left", "count": 2, "objective": "turn", "surface": "rug"}, 3)
    assert r["stopped"] is None and len(r["trials"]) == 3 and r["guard_tilt_deg"] == 40
    moves = [(s, kw) for s, _, kw in lab.tonyd.ran if s == "turn_left"]
    assert len(moves) == 3 and all(kw["guard_tilt_deg"] == 40 and kw["label"].startswith(lab.session)
                                   for _, kw in moves)
    assert [s for s, _, _ in lab.tonyd.ran].count("settle") == 4  # before each trial, and at the end
    assert lab.phone.labels == [f"exp-{t['exp']}" for t in r["trials"]] and lab.phone.tracked == 1
    assert r["summary"]["mean"] == pytest.approx(10 - 0.6)  # yaw per step - 3 * 0.2 m displacement
    rows = _ledger(lab)
    assert [x["exp"] for x in rows] == [t["exp"] for t in r["trials"]]
    assert rows[0]["episode"] == "ep-1" and rows[0]["verdict"] == "ok" and rows[0]["job"] and "score" in rows[0]["note"]
    lib = lab.library()["turn_left"]
    assert lib["incumbent"] == lib["best"] == r["cand"] and lib["surface"] == "rug"
    assert lib["candidates"][r["cand"]]["n"] == 3 and lib["evidence"] == [x["exp"] for x in rows]
    assert lab.ledger("turn_left", limit=2) == rows[1:]


def test_a_fall_is_data_get_up_and_go_on(lab):
    lab.tonyd.falls = [False, True, False]
    r = lab.batch({"skill": "walk_forward", "objective": "walk"}, 3)
    assert r["stopped"] is None and [t["verdict"] for t in r["trials"]] == ["ok", "fell", "ok"]
    assert r["summary"]["falls"] == 1 and [s for s, _, _ in lab.tonyd.ran].count("get_up") == 1


def test_stuck_on_the_floor_stops_after_two_get_ups(lab):
    lab.tonyd.falls, lab.tonyd.get_up_works = [True], False
    r = lab.batch({"skill": "wave"}, 5)
    assert len(r["trials"]) == 1 and "still lying after 2 get_up" in r["stopped"]
    assert [s for s, _, _ in lab.tonyd.ran].count("get_up") == 2


def test_preflight_refuses_and_gets_up_first(lab):
    t = lab.tonyd
    t.hot = 72
    assert "let it cool" in lab.batch("wave", 2)["stopped"] and not t.ran
    t.hot, t.battery = 45, 10800
    assert "battery" in lab.batch("wave")["stopped"] and not t.ran
    t.battery, t.latched = 11800, True
    assert "stop latch" in lab.batch("wave")["stopped"] and not t.ran
    t.latched, t.busy = False, True
    assert "busy" in lab.batch("wave")["stopped"]
    t.busy, t.lying = False, True
    assert lab.batch("wave")["stopped"] is None and t.ran[0][0] == "get_up"


def test_cancelled_move_ends_the_batch(lab):
    lab.tonyd.cancel = True
    r = lab.batch("wave", 4)
    assert len(r["trials"]) == 1 and "cancelled" in r["stopped"]


def test_cancelled_move_skips_the_closing_settle(lab):
    """Another brain moved (or a plain stop): settling now would preempt its move."""
    lab.tonyd.cancel = True
    lab.batch("wave", 3)
    assert [s for s, _, _ in lab.tonyd.ran] == ["settle", "wave"]


def test_autonomy_preempt_stops_the_batch_between_moves(lab, tmp_path):
    import tonypi.brain

    lab.preempt_path = tmp_path / "preempt"
    real_job = lab.tonyd._job

    def job(skill, count, kw, frames=None):  # the session is ended while the second trial's move runs
        out = real_job(skill, count, kw, frames)
        if [s for s, _, _ in lab.tonyd.ran].count("wave") == 2:
            lab.preempt_path.write_text(f"{time.time()}")
        return out
    lab.tonyd._job = job
    r = lab.batch("wave", 5)
    assert len(r["trials"]) == 2 and "preempted" in r["stopped"]
    assert [s for s, _, _ in lab.tonyd.ran] == ["settle", "wave", "settle", "wave"]  # no third trial, no closing settle
    lab.tonyd.ran.clear()
    assert lab.batch("wave", 1)["stopped"] is None  # a batch started after the mark runs
    assert tonypi.brain.preempt_file().name == "autonomy-preempt"


def test_setup_measures_get_up_from_lying(lab):
    lab.tonyd.call = (lambda orig: lambda op, timeout=10, **kw: (
        {"details": {k: {"motions": [f"*{k}"]} for k in ("get_up", "lie_down", "wave")}} if op == "skills"
        else orig(op, timeout, **kw)))(lab.tonyd.call)
    r = lab.batch({"skill": "get_up", "setup": "lie_down", "objective": "recover"}, 2)
    assert r["stopped"] is None and r["key"] == "get_up@lie_down" and r["guard_tilt_deg"] is None
    # no settle and no preflight get_up before a trial; the batch still ends settled
    assert [s for s, _, _ in lab.tonyd.ran] == ["lie_down", "get_up", "lie_down", "get_up", "settle"]
    rows = _ledger(lab)
    assert all(x["metrics"]["recovered"] and x["verdict"] == "ok" for x in rows)
    lab.tonyd.get_up_works = False
    lab.tonyd.ran.clear()
    r = lab.batch({"skill": "get_up", "setup": "lie_down", "objective": "recover"}, 1)
    assert r["trials"][0]["verdict"] == "fell" and not _ledger(lab)[-1]["metrics"]["recovered"]
    with pytest.raises(LabError, match="setup"):
        lab.prepare({"skill": "wave", "setup": "fly"})


def test_trial_records_actual_start_and_setup_trace(lab):
    original_call, original_job = lab.tonyd.call, lab.tonyd._job

    def call(op, timeout=10, **kw):
        if op == "skills":
            return {"details": {k: {"motions": [f"*{k}"]} for k in ("get_up", "lie_down", "wave")}}
        return original_call(op, timeout, **kw)

    def job(skill, count, kw, frames=None):
        result = original_job(skill, count, kw, frames)
        if kw.get("trace"):
            result["trace"] = f"/traces/{result['id']}.jsonl"
        return result

    lab.tonyd.call, lab.tonyd._job = call, job
    lab.batch({"skill": "get_up", "setup": "lie_down", "objective": "recover", "trace": True}, 1)
    row = _ledger(lab)[0]
    assert row["before"] == {"lying": True, "tilt_deg": 88.0}
    assert row["setup"]["skill"] == "lie_down" and row["setup"]["job"]["state"] == "done"
    assert row["setup"]["job"]["id"] != row["job"]
    assert row["setup"]["job"]["trace"] == "/traces/1.jsonl"
    assert lab.tonyd.ran[0][2]["trace"] is True
    assert row["metrics"]["recovery_goal_eligible"]


def test_trial_preserves_unknown_start_measurements_without_setup(lab):
    state = lab.tonyd.state
    lab.tonyd.state = lambda fresh=False: {k: v for k, v in state(fresh).items() if k not in ("lying", "tilt_deg")}
    lab.batch({"skill": "wave", "start": "as_is", "objective": "recover"}, 1)
    row = _ledger(lab)[0]
    assert row["before"] == {"lying": None, "tilt_deg": None} and row["setup"] is None
    assert not row["metrics"]["recovery_goal_eligible"]


def test_trial_retains_signed_trace_evidence_before_files_are_pruned(lab, tmp_path):
    original_call, original_job = lab.tonyd.call, lab.tonyd._job
    setup_trace, trial_trace = tmp_path / "setup.jsonl", tmp_path / "trial.jsonl"

    def call(op, timeout=10, **kw):
        if op == "skills":
            return {"details": {k: {"motions": [f"*{k}"]} for k in ("get_up", "lie_down")}}
        return original_call(op, timeout, **kw)

    def job(skill, count, kw, frames=None):
        result = original_job(skill, count, kw, frames)
        if skill in ("lie_down", "get_up"):
            path = setup_trace if skill == "lie_down" else trial_trace
            path.write_text('{"job":1}\ninvalid\n[]\n{"pitch":NaN}\n{"pitch":true}\n'
                            '{"t":0.02,"pitch":-88.0,"tilt":88.0}\n'
                            '{"t":0.04,"pitch":-91.0,"tilt":91.0}\n')
            result["trace"] = str(path)
        if skill == "get_up":
            setup_trace.unlink()
        return result

    lab.tonyd.call, lab.tonyd._job = call, job
    original_stop = lab.phone.record_stop
    lab.phone.record_stop = lambda: trial_trace.unlink() or original_stop()
    lab.batch({"skill": "get_up", "setup": "lie_down", "objective": "recover", "trace": True}, 1)
    row = _ledger(lab)[0]
    summary = {"pitch_samples": 2, "first": {"t": 0.02, "pitch": -88.0, "tilt": 88.0},
               "last": {"t": 0.04, "pitch": -91.0, "tilt": 91.0}, "pitch_min_deg": -91.0,
               "pitch_max_deg": -88.0}
    assert row["trace_summary"] == row["setup"]["trace_summary"] == summary
    assert not setup_trace.exists() and not trial_trace.exists()


@pytest.mark.parametrize("contents", [None, "bad\n{}\n[]\n", b"\xff\xfe"])
def test_unavailable_or_unusable_trace_does_not_interrupt_trial(lab, tmp_path, contents):
    path = tmp_path / "trace.jsonl"
    if contents is not None:
        path.write_bytes(contents if isinstance(contents, bytes) else contents.encode())
    original_job = lab.tonyd._job
    lab.tonyd._job = lambda *args: {**original_job(*args), "trace": str(path)}
    result = lab.batch({"skill": "wave", "trace": True}, 1)
    assert result["stopped"] is None
    summary = _ledger(lab)[0]["trace_summary"]
    if contents is None or isinstance(contents, bytes):
        assert summary is None
    else:
        assert summary["pitch_samples"] == 0


def test_batch_marks_the_robot_lock_as_the_labs(lab):
    from tonypi.voice.autonomy import robot_lock_holder

    seen, real = [], lab.tonyd.run
    lab.tonyd.run = lambda *a, **kw: seen.append(robot_lock_holder(lab.lock_path)) or real(*a, **kw)
    lab.batch("wave")
    assert set(seen) == {"lab"} and robot_lock_holder(lab.lock_path) == ""


def test_robot_lock_held_refuses(lab):
    fd = os.open(lab.lock_path, os.O_RDWR | os.O_CREAT)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        with pytest.raises(LabError, match="robot lock"):
            lab.batch("wave")
        assert not lab.tonyd.ran
    finally:
        os.close(fd)
    assert lab.batch("wave")["stopped"] is None


def test_no_phone_or_mounted_phone_scores_without_it(lab):
    lab.phone = FakePhone(mode="mounted")
    r = lab.batch({"skill": "walk_forward", "count": 2, "objective": "walk"}, 1)
    assert lab.phone.labels == [] and r["trials"][0]["disp_m"] is None
    assert _ledger(lab)[0]["metrics"]["basis"] == "steps"
    lab.phone = None
    assert lab.batch("wave")["stopped"] is None


def test_frames_and_base_gait_candidates(lab):
    r = lab.batch({"frames": TURN["frames"], "count": 3}, 1)
    plays = [(kw, n) for s, n, kw in lab.tonyd.ran if s == "_play"]
    assert r["cand"] == gait.digest(TURN) and len(plays) == 1
    r2 = lab.batch({"base": "turn_left", "gait": {"amplitude": 1.2}, "objective": "turn"}, 1)
    assert r2["key"] == "turn_left" and r2["cand"] == gait.digest(gait.amplitude(TURN, 1.2))
    with pytest.raises(LabError, match="exactly one"):
        lab.prepare({"skill": "wave", "base": "turn_left"})
    with pytest.raises(LabError, match="unknown skill"):
        lab.prepare("fly")


def test_compare_interleaves_and_promotes_a_better_candidate(lab):
    lab.tonyd.yaws = {"turn_left": [8.0, 8.5, 7.5, 8.2, 7.8], "_play": [12.0, 12.5, 11.5, 12.2, 11.8]}
    r = lab.compare("turn_left", {"base": "turn_left", "gait": {"amplitude": 1.2}}, 5, "turn")
    assert [t["side"] for t in r["trials"][:4]] == ["base", "candidate", "base", "candidate"]
    assert [s for s, _, _ in lab.tonyd.ran if s in ("turn_left", "_play")][:4] == ["turn_left", "_play"] * 2
    assert r["verdict"] == "promote" and r["candidate"]["mean"] > r["base"]["mean"]
    lib = lab.library()["turn_left"]
    assert lib["promote_suggested"]["over"] == r["base"]["cand"] and lib["best"] == r["candidate"]["cand"]
    assert len(lib["candidates"]) == 2


def test_compare_verdicts():
    s = lambda n, mean, std, falls=0: {"n": n, "mean": mean, "std": std, "falls": falls}  # noqa: E731
    assert Lab.verdict(s(5, 1.0, 0.1), s(5, 1.5, 0.1)) == "promote"
    assert Lab.verdict(s(5, 1.0, 0.1), s(5, 1.05, 0.1)) == "no clear difference"
    assert Lab.verdict(s(5, 1.0, 0.1), s(5, 0.5, 0.1)) == "worse"
    assert Lab.verdict(s(5, 1.0, 0.1), s(5, 2.0, 0.1, falls=1)).startswith("reject")
    assert Lab.verdict(s(3, 1.0, 0.1), s(3, 2.0, 0.1)).startswith("more trials")


def test_sweep_lies_down_maps_joints_and_gets_up(lab):
    lab.tonyd.blocked = {3: (450, 1000)}  # the knee can't bend back past 450
    lab.tonyd.pos[3] = 500
    r = lab.sweep([4, 3])
    ran = [s for s, _, _ in lab.tonyd.ran]
    assert r["stopped"] is None and ran[0] == "lie_down" and ran[-1] == "get_up" and ran.count("_play") == 8
    hip, knee = r["joints"][4], r["joints"][3]
    assert hip["sign"] == 1 and hip["range_seen"] == [350, 650] and hip["max_error"] == 0 and hip["pose"] == "lying"
    assert knee["minus"]["error"] == -100 and knee["range_seen"] == [450, 650]  # wanted 350, got to 450
    assert knee["sign"] == 1 and knee["max_error"] == 100
    model = lab.body_model()
    assert model["joints"]["4"]["n"] == 1 and model["joints"]["4"]["name"] == "left_hip_pitch"
    assert lab.sweep([4])["stopped"] is None and lab.body_model()["joints"]["4"]["n"] == 2
    assert [x["kind"] for x in _ledger(lab)] == ["sweep"] * 3


def test_sweep_standing_arms_and_refusal(lab):
    r = lab.sweep(lying=False)
    assert list(r["joints"]) == [6, 7, 8, 14, 15, 16] and lab.tonyd.ran[0][0] == "stand"
    ramps = [kw for s, _, kw in lab.tonyd.ran if s == "_play" and "back" not in kw["label"]]
    assert len(ramps) == 12 and all(kw["guard_tilt_deg"] == 30 for kw in ramps)
    lab.tonyd.ran.clear()
    lab.tonyd.hot = 75
    assert "cool" in lab.sweep([4])["stopped"] and not lab.tonyd.ran  # refused before any move


# ---- front ends ----------------------------------------------------------------------------------------------------

def test_mcp_wait_motion_returns_the_finished_job(monkeypatch):
    from tonypi import mcp_server

    class T:
        def state(self, fresh=False):
            return {"busy": False, "job": None, "tilt_deg": 2.0, "lying": False}

        def job(self, job_id=None):
            return {"id": job_id or 7, "state": "done", "max_tilt_deg": 9.0}

    monkeypatch.setattr(mcp_server, "tonyd", T())
    r = mcp_server.wait_motion(0.1)
    assert r["busy"] is False and r["job"] == {"id": 7, "state": "done", "max_tilt_deg": 9.0}
    assert mcp_server.get_job(3)["id"] == 3


def test_mcp_learning_tools_read_the_records(monkeypatch, tmp_path):
    import tonypi.brain
    from tonypi import mcp_server

    monkeypatch.setattr(tonypi.brain, "LEARN_DIR", tmp_path)
    (tmp_path / "ledger.jsonl").write_text('{"exp": "a", "key": "wave"}\nnot json\n{"exp": "b", "key": "bow"}\n')
    assert [r["exp"] for r in mcp_server.ledger()] == ["a", "b"]
    assert [r["exp"] for r in mcp_server.ledger(skill="bow")] == ["b"]
    assert mcp_server.library() == {} and mcp_server.body_model() == {}


def test_cli_reads_without_the_robot(monkeypatch, tmp_path, capsys):
    import importlib.util

    import tonypi.brain

    monkeypatch.setattr(tonypi.brain, "LEARN_DIR", tmp_path)
    (tmp_path / "library.json").write_text('{"wave": {"best": "abc"}}')
    spec = importlib.util.spec_from_file_location("learn_cli", DATA_DIR.parents[1] / "tools" / "learn.py")
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    assert cli.main(["library"]) == 0 and json.loads(capsys.readouterr().out) == {"wave": {"best": "abc"}}
    assert cli.main(["ledger"]) == 0 and json.loads(capsys.readouterr().out) == []


def test_mcp_say_prefers_the_voice_socket_then_the_agent(monkeypatch, tmp_path):
    import socket
    import threading

    from tonypi import mcp_server

    got = []

    def serve(path):
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(str(path))
        srv.listen(1)

        def run():
            conn, _ = srv.accept()
            with conn:
                got.append((path.name, json.loads(conn.makefile("r").readline())))
                conn.sendall(b'{"ok": true}\n')
            srv.close()

        threading.Thread(target=run, daemon=True).start()

    voice, agent = tmp_path / "voice.sock", tmp_path / "agent.sock"
    monkeypatch.setattr(mcp_server, "VOICE_SOCK", str(voice))
    monkeypatch.setattr(mcp_server, "AGENT_SOCK", str(agent))
    serve(agent)  # live mode is down: the local agent speaks
    assert mcp_server.say("hi") == {"said": "hi"}
    serve(voice)
    assert mcp_server.say("yo") == {"said": "yo"}
    assert got == [("agent.sock", {"op": "say", "text": "hi"}), ("voice.sock", {"op": "say", "text": "yo"})]
    with pytest.raises(Exception, match="no speech service"):
        mcp_server.say("nobody")
