"""End to end through real pi (--mode rpc) and our extension, with a scripted model (pi_fake_brain.ts) and a fake
tonyd socket: a steer during a 3-step walk lands at the next step boundary, and the model then does the new task
instead of the old one. Skipped where pi isn't installed."""

from __future__ import annotations

import json
import os
import shutil
import socket
import threading
import time
from pathlib import Path

import pytest

from tonypi.pi_front import rpc

pytestmark = pytest.mark.skipif(shutil.which("pi") is None, reason="pi not installed")
STEP_S = 0.6


class FakeTonyd:
    """Just enough of tonyd: run (with wait, cancellable), stop, skills, state."""

    def __init__(self, path: str) -> None:
        self.path = path
        self.runs: list[tuple[float, str, int]] = []
        self.plays: list[dict] = []
        self.stops: list[float] = []
        self.cancel = threading.Event()
        self.t0 = time.monotonic()
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(path)
        srv.listen(8)
        threading.Thread(target=self._accept, args=(srv,), daemon=True).start()

    def _accept(self, srv) -> None:
        while True:
            conn, _ = srv.accept()
            threading.Thread(target=self._client, args=(conn,), daemon=True).start()

    def _client(self, conn) -> None:
        try:
            self._serve(conn)
        except OSError:  # pi closed the socket (end of test)
            pass

    def _serve(self, conn) -> None:
        for line in conn.makefile("r"):
            msg = json.loads(line)
            op, rid = msg["op"], msg.get("rid")
            if op == "skills":
                details = {"wave": {"description": "wave", "source": "builtin"},
                           "my_turn": {"motions": ["*my_turn_step"], "description": "user turn", "source": "user"}}
                conn.sendall((json.dumps({"rid": rid, "ok": True, "skills": {k: v["description"] for k, v in details.items()},
                                          "details": details}) + "\n").encode())
                continue
            if op == "play":
                self.plays.append(msg)
                reply = {"job": {"id": 99, "skill": "_play", "count": 1, "said": [], "error": None, "state": "done",
                                 "seconds": 0.3, "max_tilt_deg": 4.2}}
                conn.sendall((json.dumps({"rid": rid, "ok": True, **reply}) + "\n").encode())
                continue
            if op == "run":
                self.cancel.clear()
                self.runs.append((round(time.monotonic() - self.t0, 2), msg["skill"], msg["count"]))
                ok = not self.cancel.wait(STEP_S * msg["count"])
                job = {"id": len(self.runs), "skill": msg["skill"], "count": msg["count"], "said": [], "error": None,
                       "state": "done" if ok else "cancelled", "seconds": None}
                reply = {"job": job}
            elif op == "stop":
                self.stops.append(round(time.monotonic() - self.t0, 2))
                self.cancel.set()
                reply = {"stopped": True}
            else:
                reply = {"state": {"busy": False}}
            conn.sendall((json.dumps({"rid": rid, "ok": True, **reply}) + "\n").encode())


def start_pi(tmp_path: Path, monkeypatch, body_sock: str):
    monkeypatch.setenv("TONYD_SOCK", body_sock)
    monkeypatch.setattr(rpc, "SESSION_DIR", tmp_path / "sessions")
    fake_brain = str(Path(__file__).with_name("pi_fake_brain.ts"))
    monkeypatch.setattr(rpc, "pi_command", lambda model: [
        "pi", "--mode", "rpc", "--offline", "--model", model, "--no-builtin-tools", "--no-context-files",
        "--no-skills", "--no-extensions", "--no-mcp", "-e", str(rpc.EXTENSION), "-e", fake_brain,
        "--no-session"])
    events: list[dict] = []
    settled = threading.Event()

    def on_event(ev: dict) -> None:
        events.append(ev)
        if ev.get("type") == "agent_settled":
            settled.set()

    pi = rpc.PiRpc(on_event, model="fake/brain")
    pi.start()
    return pi, events, settled


def test_studio_play_and_a_saved_stepped_skill(tmp_path: Path, monkeypatch):
    body = FakeTonyd(str(tmp_path / "tonyd.sock"))
    pi, events, settled = start_pi(tmp_path, monkeypatch, str(tmp_path / "tonyd.sock"))
    try:
        pi.send("prompt", message="try the new turn")
        assert settled.wait(30), "pi never settled"
    finally:
        pi.close()
    assert body.plays and body.plays[0]["frames"] == [{"ms": 300, "pos": {"5": 520, "13": 480}}]
    assert [(s, c) for _, s, c in body.runs if s == "my_turn"] == [("my_turn", 1), ("my_turn", 1)]  # one step per job
    results = {e["toolName"]: e["result"]["content"][0]["text"] for e in events if e.get("type") == "tool_execution_end"}
    assert "max tilt 4.2" in results["play_frames"], results


def test_steer_mid_walk_turns_right_instead(tmp_path: Path, monkeypatch):
    sock = str(tmp_path / "tonyd.sock")
    body = FakeTonyd(sock)
    monkeypatch.setenv("TONYD_SOCK", sock)
    monkeypatch.setenv("TONYPI_PI_SESSIONS", str(tmp_path / "sessions"))
    monkeypatch.setattr(rpc, "SESSION_DIR", tmp_path / "sessions")
    fake_brain = str(Path(__file__).with_name("pi_fake_brain.ts"))
    monkeypatch.setattr(rpc, "pi_command", lambda model: [
        "pi", "--mode", "rpc", "--offline", "--model", model, "--no-builtin-tools", "--no-context-files",
        "--no-skills", "--no-extensions", "--no-mcp", "-e", str(rpc.EXTENSION), "-e", fake_brain,
        "--no-session"])
    events: list[dict] = []
    settled = threading.Event()

    def on_event(ev: dict) -> None:
        events.append(ev)
        if ev.get("type") == "agent_settled":
            settled.set()

    pi = rpc.PiRpc(on_event, model="fake/brain")
    pi.start()
    try:
        pi.send("prompt", message="walk forward 3 steps, then turn left")
        deadline = time.monotonic() + 20
        while not body.runs and time.monotonic() < deadline:
            time.sleep(0.02)
        assert body.runs, "the walk never started"
        time.sleep(STEP_S * 0.5)  # mid first step
        t_steer = time.monotonic() - body.t0
        pi.send("steer", message="actually, turn right instead")
        assert settled.wait(30), "pi never settled"
        deadline = time.monotonic() + 5
        while body.runs[-1][1] != "settle" and time.monotonic() < deadline:
            time.sleep(0.02)
    finally:
        pi.close()

    print("runs", body.runs, "stops", body.stops, "steer at", round(t_steer, 2))
    skills = [s for _, s, _ in body.runs]
    assert skills.count("walk_forward") == 1, body.runs  # the first step finishes, the other two never start
    assert "turn_left" not in skills
    assert skills[-2:] == ["turn_right", "settle"], body.runs  # settles once pi is done
    t_turn = next(t for t, s, _ in body.runs if s == "turn_right")
    assert t_turn - t_steer < STEP_S + 1.5, f"steer took {t_turn - t_steer:.2f} s to land"
    results = [e for e in events if e.get("type") == "tool_execution_end" and e["toolName"] == "run_skill"]
    walk = results[0]["result"]["content"][0]["text"]
    assert "1 of 3 steps" in walk and "new message" in walk, walk
