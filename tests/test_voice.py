import asyncio
import json

from tonypi.voice import service


def test_realtime_sessions_disable_automatic_acknowledgement_filler(monkeypatch):
    from tonypi.voice import live

    # Exercise both actual session-start builders without opening audio, sockets or files.
    for cls in (service.Live, live.LiveBrain):
        session = object.__new__(cls)
        session.tid = "test-thread"
        if cls is live.LiveBrain:
            monkeypatch.setattr(cls, "_initial_items", lambda self: [])
        params = session.realtime_params("test-sdp")
        assert params["delegationAckFiller"] is False
        assert params["clientManagedHandoffs"] is (cls is service.Live)


class _Writer:
    def __init__(self):
        self.lines = []

    def write(self, b):
        self.lines.append(json.loads(b))


def test_requests_go_to_the_attached_brain_and_replies_to_their_delegation():
    async def run():
        hub = service.Hub(live_enabled=False)
        appended = []
        hub.live = type("L", (), {"append": lambda self, item, text, speak=True: appended.append((item, text)) or True,
                                  "speak": lambda self, text: True})()
        w = _Writer()
        await hub.handle({"op": "attach", "name": "pi"}, w)
        hub.on_request("walk to the chair", "item-1")
        heard = w.lines[-1]
        assert heard["event"] == "heard" and heard["text"] == "walk to the chair"
        await hub.handle({"op": "reply", "id": heard["id"], "text": "On my way.", "final": False}, w)
        await hub.handle({"op": "reply", "id": heard["id"], "text": "I'm at the chair.", "final": True}, w)
        assert appended == [("item-1", "On my way."), ("item-1", "I'm at the chair.")]
        assert heard["id"] not in hub.requests
        w2 = _Writer()
        await hub.handle({"op": "attach", "name": "own"}, w2)  # last attach wins; the old brain is told
        assert w.lines[-1] == {"event": "detached"}
    asyncio.run(run())


def test_ask_resolves_from_the_transcript_after_a_pause(monkeypatch):
    async def run():
        hub = service.Hub(live_enabled=False)
        hub.live = type("L", (), {"append": lambda *a, **k: True, "speak": lambda self, text: True})()
        hub.mode = "gpt-live"
        task = asyncio.create_task(hub.handle({"op": "ask", "text": "What should I do?", "timeout_s": 5}, _Writer()))
        await asyncio.sleep(1.1)  # past the 1 s "my own question" window
        for piece in [" Try", " going", " around", " it", " on the", " left", "."]:
            hub.on_transcript(piece)
            await asyncio.sleep(0.05)
        return await task
    assert asyncio.run(run()) == {"answer": "Try going around it on the left."}


def test_switch_command_is_handled_by_the_voice_service(monkeypatch):
    switched = []
    monkeypatch.setattr(service, "request_switch", switched.append)

    async def run():
        hub = service.Hub(live_enabled=False)
        hub.live = type("L", (), {"append": lambda *a, **k: True, "speak": lambda self, text: True})()
        w = _Writer()
        await hub.handle({"op": "attach", "name": "own"}, w)
        hub.on_request("Ciao, switch to the pie", "item-9")
        await asyncio.sleep(3.2)
        return w.lines
    lines = asyncio.run(run())
    assert switched == ["pi"] and not any(l.get("event") == "heard" for l in lines)  # never reaches the brain


def test_bare_stop_stops_the_body_without_any_model(monkeypatch):
    stopped = []
    import tonypi.tonyd.client as tc

    monkeypatch.setattr(tc.TonydClient, "__init__", lambda self, *a, **k: None)
    monkeypatch.setattr(tc.TonydClient, "stop", lambda self, latch=False: stopped.append(1))

    async def run():
        hub = service.Hub(live_enabled=False)
        w = _Writer()
        await hub.handle({"op": "attach", "name": "pi"}, w)
        hub.on_transcript(" Stop")
        hub.on_transcript(".")
        await asyncio.sleep(0.3)
        return w.lines
    lines = asyncio.run(run())
    assert stopped == [1] and any(l.get("text") == "stop" and l.get("stopped_body") for l in lines)
