"""Debug mode: toggled by voice/typed command, plays back the recording and narrates every step in order."""

import itertools
import threading

from tonypi.agent.brain import Plan, debug_command
from tonypi.agent.daemon import Agent


def test_debug_phrases():
    for on in ("enable debug mode", "turn on debugging", "debug mode on", "please start de bug mode"):
        assert debug_command(on) is True, on
    for off in ("disable debug mode", "stop debug mode", "turn off de bug", "debug off"):
        assert debug_command(off) is False, off
    for other in ("dance for us", "stop dancing", "what is a bug?"):
        assert debug_command(other) is None, other


class _Speaker:
    def __init__(self, log):
        self.log = log

    def say(self, s):
        self.log.append(("say", s))

    def play_wav(self, wav):
        self.log.append(("play", len(wav)))

    def stop(self):
        pass


class _Body:
    def __init__(self, log):
        self.log, self.cancelled = log, threading.Event()

    def run(self, name, count, say):
        self.log.append(("run", name))
        return True

    def settle(self):
        pass

    def cancel(self):
        pass


class _Brain:
    def __init__(self):
        self.streamed = None

    def plan_stream(self, text, audio, on_action):
        self.streamed = on_action is not None
        return Plan([("dance", 1)], "Let's dance!")


def _agent(debug):
    log = []
    a = Agent.__new__(Agent)
    a.body, a.speaker, a.brain, a.listener = _Body(log), _Speaker(log), _Brain(), None
    a.emit, a.debug, a.ids = (lambda *x, **k: None), debug, itertools.count(1)
    return a, log


def test_debug_narrates_in_order():
    a, log = _agent(debug=True)
    a._execute(1, "dance for us", None, voice=True, recording=b"RIFF....")
    assert log == [("say", "Playing back audio."), ("play", 8), ("say", "Audio processed as: dance for us"),
                   ("say", "Let's dance!"), ("say", "Starting dance."), ("run", "dance"), ("say", "Dance finished.")]
    assert a.brain.streamed is False  # whole plan first, so the narration can't race the moves


def test_no_narration_when_off():
    a, log = _agent(debug=False)
    a._execute(1, "dance for us", None, voice=True, recording=b"RIFF....")
    assert ("run", "dance") in log and not any(e[0] == "play" for e in log)
    assert [s for k, s in log if k == "say"] == ["Let's dance!"]
    assert a.brain.streamed is True


def test_toggle_by_command():
    a, log = _agent(debug=False)
    a._announce = lambda s: log.append(("announce", s))
    a.submit("enable debug mode", "voice")
    assert a.debug and ("announce", "Debug mode on.") in log
    a.submit("stop debug mode", "terminal")  # must not be taken as "stop"
    assert not a.debug and ("announce", "Debug mode off.") in log
