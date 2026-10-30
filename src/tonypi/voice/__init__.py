"""tonypi-voice: the shared voice peripheral (GPT-Live-1 over Codex realtime, local wake word as fallback)."""

import os

SOCK = os.environ.get("TONYPI_VOICE_SOCK", "/run/tonypi/voice.sock")
