import asyncio
import json

from tonypi.voice import service


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
