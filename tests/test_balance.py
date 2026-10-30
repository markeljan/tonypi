"""Offline evidence aggregation only; no robot or phone runtime imports."""

import json
import math

import pytest

from tonypi.learn.balance import main, summarize, zero_failure_upper95


def row(exp="one", cand="hash", **updates):
    value = {"kind": "trial", "exp": exp, "cand": cand, "label": "balance-a", "count": 1,
             "guard_tilt_deg": 30, "verdict": "ok", "episode": "ep-" + exp,
             "metrics": {"state": "done", "fell": False, "max_tilt_deg": 4, "end_tilt_deg": 2, "seconds": 8}}
    value.update(updates)
    return value


def test_zero_failure_bound_and_observed_not_guaranteed():
    group = summarize([row(str(i)) for i in range(5)])["groups"][0]
    assert group["n"] == group["completed"] == 5
    assert group["falls"] == group["failures"] == 0
    assert group["zero_failure_upper95"] == pytest.approx(0.4507197283)
    assert group["peak_tilt_deg"] == {"n": 5, "mean": 4, "std": 0, "min": 4, "max": 4}
    assert "falls remain possible" in group["evidence_limit"]
    assert group["contact_unverified_trials"] == 5
    assert zero_failure_upper95(0) is None
    assert summarize([])["groups"] == []
    for invalid in (-1, 1.5, True):
        with pytest.raises(ValueError):
            zero_failure_upper95(invalid)


def test_guard_without_fall_and_incomplete_jobs_do_not_prove_feasible():
    rows = [row("guard", verdict="tilt_guard", metrics={"state": "failed", "fell": False, "max_tilt_deg": 31}),
            row("cancel", verdict="cancelled", metrics={"state": "cancelled", "fell": False}),
            row("unknown", metrics={"state": "done", "fell": False}),
            row("fall", verdict="fell", metrics={"state": "done", "fell": True, "max_tilt_deg": 60}), row()]
    group = summarize(rows)["groups"][0]
    assert [group[k] for k in ("n", "completed", "falls", "guard_failures", "failures")] == [5, 1, 1, 1, 4]
    assert group["zero_failure_upper95"] is None
    assert group["peak_tilt_deg"]["max"] == 60
    assert group["completed_peak_tilt_deg"]["max"] == 4


def test_hashes_counts_guards_and_starting_conditions_never_pool():
    rows = [row("a"), row("b", cand="other"), row("c", count=2), row("d", guard_tilt_deg=40), row("e")]
    manifest = {"a": {"surface": "wood", "start": "stand"}, "e": {"surface": "rug", "start": "stand"}}
    groups = summarize(rows, manifest)["groups"]
    assert len(groups) == 5
    assert all(g["n"] == 1 for g in groups)
    assert {g["surface"] for g in groups} == {None, "wood", "rug"}


def test_setup_job_ids_do_not_split_same_start_condition():
    rows = [row("a", setup={"skill": "squat", "job": {"id": 1}}),
            row("b", setup={"skill": "squat", "job": {"id": 3}})]
    group, = summarize(rows)["groups"]
    assert group["setup"] == "squat" and group["n"] == 2


def test_explicit_frame_binding_separates_changed_saved_skills():
    groups = summarize([row("a"), row("b")], {"a": {"frame_hash": "frames1"},
                                                  "b": {"frame_hash": "frames2"}})["groups"]
    assert len(groups) == 2
    assert all(g["frame_binding"] == "explicit" for g in groups)
    assert "may change" in summarize([row()])["groups"][0]["frame_binding"]


def test_contradictory_terminal_tilt_is_not_completed_feasible():
    trial = row()
    trial["metrics"]["end_tilt_deg"] = 60
    group, = summarize([trial])["groups"]
    assert group["completed"] == 0 and group["falls"] == group["failures"] == 1


def test_contact_requires_explicit_matching_phone_review_and_completion():
    review = {"reviewed": True, "source": "phone", "episode": "ep-a", "support": "right", "observed_hold_s": 3}
    rows = [row("a"), row("b"), row("c", verdict="cancelled")]
    manifest = {"a": {"contact_review": review, "commanded_hold_s": 3},
                "b": {"contact_review": review, "commanded_hold_s": 3},
                "c": {"contact_review": {**review, "episode": "ep-c"}, "commanded_hold_s": 3}}
    group, = summarize(rows, manifest)["groups"]
    assert group["contact_reviews"] == [{"exp": "a", "episode": "ep-a", "support": "right", "observed_hold_s": 3}]
    assert group["contact_unverified_trials"] == 2
    assert group["commanded_hold_s"] == 3
    assert summarize([row("a")])["groups"][0]["contact_reviews"] == []


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf, True, "4", -1])
def test_bad_measurements_are_explicitly_rejected(value):
    bad = row("bad")
    bad["metrics"]["max_tilt_deg"] = value
    result = summarize([bad, row()])
    assert len(result["rejected"]) == 1
    assert result["rejected"][0]["exp"] == "bad"
    assert result["groups"][0]["n"] == 1
    json.dumps(result, allow_nan=False)


def test_nonfinite_metadata_and_missing_hash_rejected():
    result = summarize([row("a"), row("b", cand=None)], {"a": {"starting_conditions": {"temperature": math.nan}}})
    assert result["groups"] == []
    assert len(result["rejected"]) == 2


def test_cli_filter_and_manifest(tmp_path, capsys):
    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text("\n".join(json.dumps(r) for r in [row(), row("other", label="walk"), {"kind": "sweep"}]))
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"one": {"surface": "wood", "segment": "arms held"}}))
    assert main([str(ledger), "--manifest", str(manifest), "--label-prefix", "balance"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["groups"][0]["experiments"] == ["one"]
    assert result["groups"][0]["segment"] == "arms held"
