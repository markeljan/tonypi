"""A tiny stand-in for `codex app-server` (JSON-RPC lines on stdio) for tests. Behaviour by the user's text:
"wave" -> calls run_skill(wave), then says "Hi!"; "look" -> calls snapshot, says how many images it got back;
"deny" -> 401 error then failed turn; "think" -> waits until interrupted."""

import json
import sys
import threading
import uuid

lock = threading.Lock()
pending: dict[int, threading.Event] = {}
replies: dict[int, dict] = {}
interrupted = threading.Event()


def send(msg):
    with lock:
        sys.stdout.write(json.dumps(msg) + "\n")
        sys.stdout.flush()


def call_tool(rid, thread, turn, tool, args):
    ev = pending.setdefault(rid, threading.Event())
    send({"id": rid, "method": "item/tool/call", "params": {"threadId": thread, "turnId": turn, "callId": str(rid),
                                                            "tool": tool, "arguments": args}})
    ev.wait(30)
    return replies.pop(rid)


def turn(thread, tid, text):
    send({"method": "turn/started", "params": {"threadId": thread, "turn": {"id": tid, "items": [], "status": "inProgress"}}})
    status = "completed"
    if "deny" in text:
        send({"method": "error", "params": {"threadId": thread, "turnId": tid, "willRetry": True, "error": {
            "message": "Reconnecting... 1/5", "codexErrorInfo": {"responseStreamDisconnected": {"httpStatusCode": 401}}}}})
        status = "failed"
    elif "think" in text:
        interrupted.wait(30)
        status = "interrupted"
    elif "wave" in text:
        r = call_tool(9001, thread, tid, "run_skill", {"skill": "wave"})
        send({"method": "item/completed", "params": {"item": {"type": "agentMessage", "id": "m", "text":
              "Hi! " + r["result"]["contentItems"][0]["text"]}}})
    elif "look" in text:
        r = call_tool(9002, thread, tid, "snapshot", {})
        n = sum(1 for i in r["result"]["contentItems"] if i["type"] == "inputImage")
        send({"method": "item/completed", "params": {"item": {"type": "agentMessage", "id": "m", "text": f"{n} image"}}})
    send({"method": "thread/tokenUsage/updated", "params": {"threadId": thread, "turnId": tid, "tokenUsage": {
        "last": {"inputTokens": 100, "outputTokens": 5, "cachedInputTokens": 50, "reasoningOutputTokens": 0, "totalTokens": 105},
        "total": {"inputTokens": 100, "outputTokens": 5, "cachedInputTokens": 50, "reasoningOutputTokens": 0, "totalTokens": 105}}}})
    send({"method": "turn/completed", "params": {"threadId": thread, "turn": {"id": tid, "items": [], "status": status,
          "durationMs": 1}}})


for line in sys.stdin:
    m = json.loads(line)
    if "method" not in m:  # a reply to our item/tool/call
        replies[m["id"]] = m
        pending.setdefault(m["id"], threading.Event()).set()
        continue
    meth, rid = m["method"], m.get("id")
    if meth == "initialize":
        send({"id": rid, "result": {"userAgent": "fake"}})
    elif meth == "account/read":
        send({"id": rid, "result": {"account": {"type": "chatgpt", "planType": "plus"}}})
    elif meth == "thread/start":
        assert {t["name"] for t in m["params"]["dynamicTools"]} >= {"run_skill"}
        send({"id": rid, "result": {"thread": {"id": "th1"}, "model": "fake-model"}})
    elif meth == "turn/start":
        tid = str(uuid.uuid4())
        interrupted.clear()
        send({"id": rid, "result": {"turn": {"id": tid, "items": [], "status": "inProgress"}}})
        threading.Thread(target=turn, args=("th1", tid, m["params"]["input"][0]["text"]), daemon=True).start()
    elif meth == "turn/interrupt":
        interrupted.set()
        send({"id": rid, "result": {}})
    elif meth == "turn/steer":
        send({"id": rid, "result": {"turnId": m["params"]["expectedTurnId"]}})
