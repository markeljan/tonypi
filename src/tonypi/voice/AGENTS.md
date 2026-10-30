# Voice and brain harness (tonypi-voice)

This directory is my voice and the harness around my brain. `service.py` owns the USB mic and speaker, runs the
GPT-Live-1 realtime session (codex app-server, WebRTC) and the stop reflex. `live.py` is live mode: Codex thread L
(the brain), the `tony_*` dynamic tools, background workers, the autonomy host, the conversation log. `autonomy.py`
is the autonomy scheduler (pure policy plus a tick loop). `client.py` is the voice.sock client. Docs: `docs/BRAIN.md`,
`docs/GPT_LIVE.md`, `docs/LIVE_MODE_SPEC.md` (history; BRAIN.md wins where they differ).

## Prompts (`prompts/`)
- `live_voice.md` GPT-Live's prompt (the voice). Short. It must keep "delegate every request" and never answer from
  its own knowledge.
- `codex_developer.md` L's developer instructions (<= 6 KB; sent with every request, so every byte costs latency). The
  memory snapshot is appended to it at thread start.
- `codex_realtime_start.md` / `codex_realtime_end.md` the voice protocol for L ([COMMENTARY] / [FINAL] tags).
- `codex_worker.md` background workers. `codex_autonomy.md` autonomy sessions.
- `live_core.md` and `live_m*.md` are the older modular voice prompt (variants `core`/`plain`), kept for A/B tests.
- Procedures belong in skills (`.agents/skills/`), not in prompts.

## Changing it safely
- Edits load only after a restart. From L: run `uv run pytest -q tests/test_voice.py tests/test_live_speed.py`,
  commit (`live:`), then `tony_restart_voice(reason)`. It checks imports, the prompt and the tests, and refuses within
  90 s of the last restart or while tasks run. Never `systemctl restart tonypi-voice` from inside the voice.
- Changing a `tony_*` tool definition (DYNAMIC_TOOLS) starts a new L thread at the next restart. It is seeded with a
  continuity note; put open threads in the journal first (`tony journal`).
- The stop reflex is safety code: `STOP_WORDS`, `_stop_body` and the latch/unlatch in `service.py`. Changing it needs
  the owner's yes. Stop never waits for a model.
- The GPT-Live session sleeps after 30 s idle and the wake spotter ("Tony") starts it again on the same L thread
  (`Hub.wake/sleep_voice/keep_voice`, `Live.start_voice/stop_voice`). Anything that must be heard while it sleeps
  goes through `speak()` (Piper fallback) or wakes it first (`hub.wake`). Keep "stop" in the spotter.
- Keep these hard rules: the phone_mic echo drop (a phone request Tony's own mic also heard is dropped), only Tony's
  own mic clears the stop latch, a worker result is spoken once, no relay of quiet [COMMENTARY].
- Logs: `journalctl -u tonypi-voice`, `~/.local/share/tonypi/live/conversation.jsonl`, `tasks.jsonl`, `state.json`.
- Codex config: `~/.config/tonypi/codex-live/config.toml`, identical to `provision/files/codex-live.config.toml`.
  Never run `codex login/logout`; auth is shared by `tools/share_chatgpt_login.py`.
