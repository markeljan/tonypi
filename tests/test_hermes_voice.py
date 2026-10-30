"""The tonypi-voice client (docs/GPT_LIVE.md interface) against a fake voice service socket."""

import json
import queue
import socket
import threading
import time

from test_hermes_front import FakeHermes, FakeTonyd, state_dir, wait  # noqa: F401 (fixture)

from tonypi.hermes.front import Front
from tonypi.hermes.peripherals import VoiceServicePeripheral, _speakable


class FakeVoice:
    """Accepts one brain; records what it sends; the test pushes events to it."""

    def __init__(self, path: str) -> None:
        self.got: queue.Queue = queue.Queue()
        self.srv = socket.socket(socket.AF_UNIX)
        self.srv.bind(path)
        self.srv.listen(1)
        self.conn = None
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        self.conn, _ = self.srv.accept()
        for line in self.conn.makefile("r"):
            self.got.put(json.loads(line))

    def push(self, msg: dict) -> None:
        self.conn.sendall((json.dumps(msg) + "\n").encode())

    def next(self, op: str, timeout: float = 3) -> dict:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            m = self.got.get(timeout=end - time.monotonic())
            if m.get("op") == op:
                return m
        raise AssertionError(f"no {op}")


def setup(tmp_path):
    path = str(tmp_path / "voice.sock")
    voice = FakeVoice(path)
    h = FakeHermes()
    f = Front(h, tonyd=FakeTonyd())
    VoiceServicePeripheral(f, path).start()
    assert voice.next("attach")["name"] == "hermes"
    return voice, h, f


def test_heard_goes_to_hermes_and_the_final_reply_comes_back(tmp_path):
    voice, h, f = setup(tmp_path)
    voice.push({"event": "heard", "id": 7, "text": "wave"})
    assert wait(lambda: h.started)
    h.finish()
    reply = voice.next("reply")
    assert reply == {"op": "reply", "id": 7, "text": "done run1", "final": True}


def test_stop_never_reaches_the_llm(tmp_path):
    voice, h, f = setup(tmp_path)
    voice.push({"event": "heard", "id": 1, "text": "stop"})
    assert wait(lambda: f.tonyd.stops == 1)
    assert h.started == []


def test_ask_round_trip(tmp_path):
    voice, h, f = setup(tmp_path)
    out = {}
    t = threading.Thread(target=lambda: out.update(a=f.ask("I'm stuck at the rug edge, what should I do?", 5)))
    t.start()
    ask = voice.next("ask")
    assert "rug edge" in ask["text"] and ask["timeout_s"] == 5
    voice.push({"answer": "go around it to the left"})
    t.join(3)
    assert out["a"] == "go around it to the left"


def test_typed_answer_wins_too(tmp_path):
    voice, h, f = setup(tmp_path)
    out = {}
    t = threading.Thread(target=lambda: out.update(a=f.ask("What's going on?", 5)))
    t.start()
    voice.next("ask")
    f.submit("you're on the cable", "terminal")
    t.join(3)
    assert out["a"] == "you're on the cable" and h.started == []  # an answer, not a new command


def test_long_text_is_cut_for_speech():
    text = "First sentence is here. " * 30
    out = _speakable(text)
    assert len(out) <= 400 and out.endswith(".")
