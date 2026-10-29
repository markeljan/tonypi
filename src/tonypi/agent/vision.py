"""Description of a tonyd snapshot by the on-board multimodal LFM3 server (llama-server with --mmproj, kept loaded)."""

from __future__ import annotations

import base64
import json
import os
import re
import threading
import urllib.request
from pathlib import Path

MM_URL = os.environ.get("TONYPI_MM_URL", "http://127.0.0.1:8080/v1/chat/completions")  # same LFM3-Omni server as the brain
PROMPT = ("This photo was taken by a small robot's camera, close to the floor. Describe what is in it in one or two "
          "short sentences, mentioning any people. Speak in the first person as the robot, e.g. 'I see ...'.")


def describe(image: Path, cancelled: threading.Event) -> str | None:
    """~16 s on the Pi 5. Returns None if cancelled or the server fails."""
    url = "data:image/jpeg;base64," + base64.b64encode(Path(image).read_bytes()).decode()
    body = {"messages": [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": url}},
                                                      {"type": "text", "text": PROMPT}]}],
            "max_tokens": 120, "temperature": 0, "cache_prompt": False,
            "chat_template_kwargs": {"enable_thinking": False}}
    out: list[str | None] = [None]

    def call() -> None:
        try:
            req = urllib.request.Request(MM_URL, json.dumps(body).encode(), {"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=120) as r:
                out[0] = json.load(r)["choices"][0]["message"].get("content")
        except Exception:
            out[0] = None

    t = threading.Thread(target=call, daemon=True)
    t.start()
    while t.is_alive():
        if cancelled.wait(0.2):
            return None
    return re.sub(r"[*_#`]", "", out[0] or "").strip() or None
