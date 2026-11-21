"""Description of a tonyd snapshot via Gemini Robotics ER 2 (v1beta) or local LFM3 server."""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import threading
import urllib.request
from pathlib import Path

log = logging.getLogger("tonypi.agent.vision")

MM_URL = os.environ.get("TONYPI_MM_URL", "http://127.0.0.1:8080/v1/chat/completions")
_DEFAULT_MM_URL = MM_URL
_ORIG_URLOPEN = urllib.request.urlopen

PROMPT = (
    "This photo was taken by a small robot's camera, close to the floor. Describe what is in it in one or two "
    "short sentences, mentioning any people. Speak in the first person as the robot, e.g. 'I see ...'."
)


def _should_use_gemini() -> bool:
    provider = os.environ.get("TONYPI_VISION_PROVIDER", "auto").strip().lower()
    if provider == "local":
        return False
    if provider == "gemini":
        return True
    if MM_URL != _DEFAULT_MM_URL or urllib.request.urlopen is not _ORIG_URLOPEN:
        return False
    return bool(os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"))


def describe(image: Path, cancelled: threading.Event) -> str | None:
    """Describe a camera snapshot using Gemini Robotics ER 2 or local LFM3 fallback.

    Returns None if cancelled or both providers fail.
    """
    image_bytes = Path(image).read_bytes()
    url = "data:image/jpeg;base64," + base64.b64encode(image_bytes).decode()
    body = {
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": url}},
                    {"type": "text", "text": PROMPT},
                ],
            }
        ],
        "max_tokens": 120,
        "temperature": 0,
        "cache_prompt": False,
        "chat_template_kwargs": {"enable_thinking": False},
    }
    out: list[str | None] = [None]

    def call() -> None:
        if _should_use_gemini():
            try:
                from tonypi.agent.gemini_brain import describe_image_gemini

                res = describe_image_gemini(image_bytes, prompt=PROMPT)
                if res:
                    out[0] = res
                    return
            except Exception as exc:
                log.warning("Gemini Robotics ER 2 vision unavailable (%s); falling back to local MM_URL", exc)

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
