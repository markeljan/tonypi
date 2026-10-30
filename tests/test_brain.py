"""Brain store, recall index and the `tony` CLI, on a temporary BRAIN_DIR / LIVE_DIR (no robot, no voice)."""

import json
import time

import pytest

from tonypi.brain import cli, recall, store


@pytest.fixture(autouse=True)
def brain(tmp_path, monkeypatch):
    monkeypatch.setenv("TONYPI_BRAIN_DIR", str(tmp_path / "brain"))
    monkeypatch.setenv("TONYPI_LIVE_DIR", str(tmp_path / "live"))
    (tmp_path / "live").mkdir()
    return tmp_path / "brain"


def test_ensure_seeds_and_git(brain):
    store.ensure()
    for name in ("MEMORY.md", "goals.json", "inbox.md", "autonomy.json", "curriculum.json", ".git", "journal"):
        assert (brain / name).exists(), name
    mem = (brain / "MEMORY.md").read_text()
    for section in ("## Who I am", "## Owner", "## Body", "## Home", "## Lessons", "## Recent"):
        assert section in mem
    assert len(mem.encode()) <= store.MEMORY_MAX
    goals = store.list_goals()
    assert [g["id"] for g in goals] == ["g1", "g2", "g3", "g4", "g5", "g6"]
    assert all(g["origin"] == "owner" and g["state"] == "active" for g in goals)
    assert goals[0]["needs_body"] and not goals[5]["needs_body"]
    assert json.loads((brain / "goals.json").read_text())["next"] == 7
    assert store.curriculum()[0]["id"] == "joint_map"
    assert not store.commit("nothing changed")


def test_remember_sections_recent_and_dupes(brain):
    assert store.remember("Owner likes short answers", "owner") == "Owner"
    assert store.remember("Owner likes short answers", "owner") == "known"
    for i in range(12):
        assert store.remember(f"fact number {i}") == "Recent"
    blocks = store._split((brain / "MEMORY.md").read_text())
    recent = [x for x in store._block(blocks, "Recent")[1:] if x.strip()]
    assert len(recent) == store.RECENT_MAX and recent[-1].endswith("fact number 11")
    assert "- Owner likes short answers" in store._block(blocks, "Owner")
    # everything is in the journal, also what Recent dropped
    assert any("fact number 0" in x for x in store.journal_lines())
    # a typed fact that doesn't fit under the cap lands in Recent
    assert store.remember("x" * 3000, "home") == "Recent"


def test_goals_lifecycle(brain):
    gid = store.add_goal("Balance on one leg", why="fun", success="3 s hold", needs_body=True)
    assert gid == "g7" and store.get_goal(gid)["state"] == "proposed"
    assert store.add_goal("balance on ONE leg") == gid  # open duplicate by title
    store.note_goal(gid, "first try fell at 1.2 s")
    for i in range(12):
        store.note_goal(gid, f"n{i}")
    g = store.get_goal(gid)
    assert len(g["notes"]) == store.NOTES_MAX and g["notes"][-1]["text"] == "n11" and g["last_worked"]
    g = store.update_goal(gid, state="blocked", blocked_on="no phone", evidence="ep-1")
    assert g["state"] == "blocked" and g["evidence"] == ["ep-1"]
    with pytest.raises(ValueError):
        store.update_goal(gid, colour="red")
    with pytest.raises(ValueError):
        store.update_goal(gid, state="finished")
    with pytest.raises(KeyError):
        store.update_goal("g99", state="done")
    assert any("g7 proposed -> blocked: no phone" in x for x in store.journal_lines())
    # closing a goal with a rung marks the curriculum rung done
    store.update_goal("g1", state="done")
    assert store.curriculum()[0]["done"] and store.get_goal("g1")["closed"]


def test_next_goal_order(brain):
    assert store.next_goal(True)["id"] == "g1"
    assert store.next_goal(False)["id"] == "g6"  # the only non-body owner goal
    for i in range(1, 7):
        store.update_goal(f"g{i}", state="paused")
    assert store.next_goal(True) is None
    p = store.add_goal("proposed thing", priority=1)
    a = store.add_goal("active self thing", priority=4, state="active")
    assert store.next_goal(True)["id"] == a
    assert store.next_goal(True, exclude=[a])["id"] == p
    store.update_goal("g4", state="active")
    assert store.next_goal(True)["id"] == "g4"
    assert store.next_goal(False)["id"] == a


def test_next_rung_respects_prereqs_and_open_goals(brain):
    assert store.next_rung(True) is None  # joint_map taken by g1, everything else needs it
    store.update_goal("g1", state="done")
    assert store.next_rung(True)["id"] == "weight_shift"
    assert store.next_rung(False) is None


def test_inbox_and_autonomy_state(brain):
    assert store.inbox_add("I learned to wave") == 1
    assert store.inbox_add("I learned to wave") == 1
    assert store.inbox_add("battery was low") == 2
    assert store.inbox_take() == ["I learned to wave", "battery was low"]
    assert store.inbox_take() == []
    st = store.autonomy_state()
    assert st["paused_until"] == 0 and st["day"] == time.strftime("%Y-%m-%d")
    store.set_autonomy(tokens_today=500, day="2000-01-01")
    assert store.autonomy_state()["tokens_today"] == 0  # new day
    store.set_autonomy(tokens_today=500)
    assert store.autonomy_state()["tokens_today"] == 500


def test_memory_snapshot(brain):
    store.inbox_add("tell him")
    store.journal("talk", "owner asked for a wave")
    snap = store.memory_snapshot()
    assert "## Who I am" in snap and "## Goals" in snap and "g1 [active owner p1 body]" in snap
    assert "Inbox: 1 item" in snap and "[talk] owner asked for a wave" in snap
    small = store.memory_snapshot(1500)
    assert len(small.encode()) <= 1500 and "## Goals" in small and "[...cut]" in small


def test_journal_format(brain):
    store.journal("fall", "fell\nforward  during walk")
    line = store.journal_lines()[-1]
    assert line.endswith("[fall] fell forward during walk") and line[2:7].count(":") == 1


def _jsonl(path, rows, extra=""):
    with path.open("a") as f:
        for r in rows:
            f.write((r if isinstance(r, str) else json.dumps(r)) + "\n")
        f.write(extra)


def test_recall_incremental_rotation_and_malformed(brain, tmp_path):
    live = tmp_path / "live"
    conv = live / "conversation.jsonl"
    now = time.time()
    _jsonl(conv, [{"t": now - 100, "role": "user", "text": "Tony, wave your hand"}, "{not json",
                  {"t": now - 90, "role": "assistant", "text": "I waved my hand."}],
           extra='{"t": 1, "role": "user", "text": "half writ')
    _jsonl(live / "tasks.jsonl", [{"id": "1030-t1", "status": "done", "brief": "measure the turning gait",
                                   "end": now - 50, "lesson": "turn_left yaws 10 deg per step"}])
    (brain / "learning").mkdir(parents=True, exist_ok=True)
    _jsonl(brain / "learning" / "ledger.jsonl", [{"exp": "e1", "skill": "walk", "verdict": "fell", "t": now - 40},
                                                 "garbage"])
    store.journal("fall", "fell backwards on the rug")
    hits = recall.recall("wave hand")
    assert hits and hits[0]["source"] == "talk" and "wave" in hits[0]["text"]
    assert recall.recall("turning")[0]["source"] == "task"  # porter stemming
    assert recall.recall("walk fell")[0]["source"] == "learn"
    assert recall.recall("rug")[0]["source"] == "journal"
    assert recall.recall("zebra") == []
    assert recall.recall("wave zebra")  # falls back to any word
    assert recall.recall("wave", source="journal") == []
    # the half-written line is picked up once complete; nothing is indexed twice
    with conv.open("a") as f:
        f.write('ten"}\n')
    assert [h["text"] for h in recall.recall("half written")] == ["user: half written"]
    assert len(recall.recall("waved")) == 2  # "waved" stems to "wave": both lines, once each
    # rotation: the old file keeps its inode, the new one is indexed from the start
    conv.rename(live / "conversation.old.jsonl")
    _jsonl(conv, [{"t": now, "role": "user", "text": "walk to the kitchen"}])
    assert recall.update() == 1
    assert len(recall.recall("waved")) == 2 and recall.recall("kitchen")
    # days filter
    _jsonl(conv, [{"t": now - 10 * 86400, "role": "user", "text": "ancient kitchen story"}])
    assert len(recall.recall("kitchen", days=1)) == 1 and len(recall.recall("kitchen")) == 2


def test_recall_speed(brain, tmp_path):
    conv = tmp_path / "live" / "conversation.jsonl"
    _jsonl(conv, [{"t": time.time() - i, "role": "user", "text": f"line {i} about walking and turning"}
                  for i in range(3000)])
    recall.update()
    t0 = time.perf_counter()
    assert recall.recall("walking turning", limit=8)
    assert time.perf_counter() - t0 < 0.3


def run(capsys, *argv):
    rc = cli.main(list(argv))
    return rc, capsys.readouterr().out.strip()


def test_cli(brain, capsys):
    rc, out = run(capsys, "goal", "add", "Find", "the", "red", "ball", "--body", "--success", "ball in view")
    assert rc == 0 and out == "g7"
    rc, out = run(capsys, "goal", "list")
    assert "g7 [proposed self p3 body] Find the red ball" in out
    assert run(capsys, "goal", "note", "g7", "looked", "left")[0] == 0
    rc, out = run(capsys, "goal", "update", "g7", "--next-step", "look right", "--priority", "1", "--no-body")
    assert "p1" in out and "next: look right" in out
    rc, out = run(capsys, "goal", "show", "g7", "--json")
    g = json.loads(out)
    assert g["notes"][0]["text"] == "looked left" and g["needs_body"] is False
    rc, out = run(capsys, "goal", "block", "g7", "phone", "offline")
    assert "blocked on: phone offline" in out
    assert run(capsys, "goal", "done", "g7", "seen", "at", "3", "m")[0] == 0
    assert store.get_goal("g7")["evidence"] == ["seen at 3 m"]
    assert "g7" not in run(capsys, "goal", "list")[1] and "g7" in run(capsys, "goal", "list", "--all")[1]
    assert run(capsys, "goal", "update", "g99", "--why", "x")[0] == 1
    assert run(capsys, "remember", "--kind", "lesson", "guard 30 for poses")[1] == "Lessons"
    assert run(capsys, "journal", "--kind", "code", "fixed it")[1] == "ok"
    assert "[code] fixed it" in run(capsys, "history", "--today")[1]
    assert run(capsys, "inbox", "add", "hello")[1] == "1 queued"
    assert run(capsys, "inbox", "take")[1] == "- hello"
    assert "## Goals" in run(capsys, "memory")[1]
    rc, out = run(capsys, "autonomy", "pause", "10")
    assert "paused: 9 min" in out or "paused: 10 min" in out
    run(capsys, "autonomy", "now")
    st = store.autonomy_state()
    assert st["run_now"] and st["paused_until"] == 0
    run(capsys, "autonomy", "body-pause")
    assert store.autonomy_state()["body_paused_until"] > time.time() + 1700
    run(capsys, "autonomy", "resume")
    assert store.autonomy_state()["body_paused_until"] == 0
    rc, out = run(capsys, "recall", "fixed", "--json")
    assert json.loads(out)[0]["source"] == "journal"


def test_cli_say_without_voice(brain, monkeypatch, capsys):
    monkeypatch.setattr("tonypi.voice.SOCK", str(brain.parent / "nope.sock"))
    assert cli.main(["say", "hi"]) == 1
