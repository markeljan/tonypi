# Tony's mind: architecture and runbook (2026-10-30)

One harness: **live mode**. GPT-Live-1 is the voice; a Codex app-server is the brain. It is the only harness with a
shell, web, the phone, native goals and skills. The bake-off harnesses (own, Hermes, pi) are retired to branches
`deprecated/harness-{own,hermes,pi}`; what worked there was ported (own's measured `trial` and task contract, the
pi/Hermes skill texts, pi's "few photos" lesson). The local LFM3 agent (`tonypi-agent`) stays as the offline fallback.
Agent-facing summary: `AGENTS.md`. Measurable checks: `docs/ACCEPTANCE.md`.

## Parts
```
mic/speaker ── tonypi-voice (src/tonypi/voice/service.py) ── stop reflex (STOP_WORDS → tonyd stop + latch, no LLM)
                 │ GPT-Live-1 realtime (WebRTC), prompt prompts/live_voice.md: delegates every request
                 ▼
               codex app-server, CODEX_HOME ~/.config/tonypi/codex-live (src/tonypi/voice/live.py)
                 ├ L: persistent brain thread (terra, low, fast) ← AGENTS.md + codex_developer.md + memory snapshot
                 ├ tasks: tony_start_task workers (persistent threads, terra|sol, optional Codex goal)
                 └ autonomy: one session thread at a time (terra, medium, Codex goal) ← codex_autonomy.md
               MCP: tonypi (robot + learning, :8765) · phone (iPhone bridge, :8767, optional)
tonyd (body daemon, safety reflexes) ← every move; jobs.jsonl
brain dir ~/.local/share/tonypi/brain (memory, goals, journal, learning) ← `tony` CLI, the learning runner
```

## Voice
- `tonypi-voice.service` runs live mode by default (`TONYPI_VOICE_MODE=live`), enabled at boot. GPT-Live hears and
  speaks; it hands every request to L (Codex `clientManagedHandoffs:false`, bemTags). The service bridges requests
  GPT-Live answered itself or Codex missed: fragments are merged (2 s of no new speech), 1-2 word fragments dropped,
  duplicates logged once.
- Stop: "stop / halt / freeze" stops tonyd with a latch before any model hears it, interrupts L, cancels body tasks
  and ends an autonomy session. Motion stays refused until the owner speaks again.
- Phone mic requests are labelled; one Tony's own mic also heard is dropped. Only Tony's own mic clears the latch.

## Brain L
- Persistent thread (state in `~/.local/share/tonypi/live/state.json`). Developer instructions =
  `prompts/codex_developer.md` + the memory snapshot (`tony memory`). Project doc = root `AGENTS.md`.
- Rotated to a new thread when older than 20 h or its context passes 90k input tokens (checked at service start); the
  new one gets a continuity note pointing at the brain dir and the journal. Compaction at 80k with a robot-specific
  `compact_prompt`.
- Short turns: quick body work and corrections itself; anything over ~20 s goes to a task.

## Background tasks
`tony_start_task(title, brief, uses_body, effort, tier, goal)`: a persistent worker thread (`codex_worker.md`), tier
terra or sol, `goal` = measurable success criteria set as the thread's Codex goal. Result: status
done|partial|failed|blocked, spoken_sentence (spoken once), details, lesson, commits, needs_voice_restart, question
(owner-only decisions). Each task is a line in `live/tasks.jsonl` and the journal; a lesson goes to MEMORY.md.
Task ids persist across restarts (`1030-t7`).

## Autonomy (`src/tonypi/voice/autonomy.py`)
- Every 15 s: if nobody spoke and L was idle for 75 s, no body task runs, no session runs, 90 s since the last one,
  tokens today < 5M and the rate-limit windows are under 70 % / 60 %, start a session.
- Goal: `store.next_goal(body_ok)`: owner goals first by priority, then active self goals, then proposed self goals;
  none → the next unmet curriculum rung or a non-body self-review. At most 4 sessions per goal per day.
- body_ok: battery >= 11.0 V (or unknown with the tether on), hottest servo < 70 C, no stop latch, not body-paused.
- Session: a new thread (`codex_autonomy.md` + snapshot), `thread/goal/set {objective, tokenBudget 400k}`, 15 min wall
  cap. Ends on goal complete/blocked/budget, 60 s without a turn, the cap, or owner speech (within 2 s: goal paused,
  turn interrupted, body stopped without a latch). A spoken stop also sets `body_paused_until` = now + 30 min.
- Owner control (via L or a shell): `tony autonomy pause [MIN]`, `body-pause [MIN]`, `resume`, `now`, `status`.

## Memory (`tonypi.brain`, BRAIN_DIR = `~/.local/share/tonypi/brain`, a local git repo, never pushed)
| File | What |
|---|---|
| `MEMORY.md` | <= 4 KB: Who I am, Owner, Body, Home, Lessons, Recent (<= 10 lines) |
| `goals.json` | goal ledger: id, title, why, origin owner/self, priority 1-5, state, success, next_step, notes (10), attempts, evidence |
| `journal/YYYY-MM-DD.md` | `- HH:MM [kind] text` (talk, task, goal, autonomy, learn, fall, code, note) |
| `inbox.md` | things to tell the owner next time |
| `autonomy.json` | paused_until, body_paused_until, run_now, tokens and sessions today |
| `curriculum.json` | skill ladder with prerequisites and measurable targets (docs/BODY_LEARNING.md) |
| `learning/` | `ledger.jsonl`, `body_model.json`, `library.json` (src/tonypi/learn) |
| `history.sqlite` | FTS5 index over conversations, journals, tasks and the ledger (`tony recall`) |
Other data: `~/.local/share/tonypi/live/` (conversation.jsonl, tasks.jsonl, state.json),
`~/.local/share/tonypi/jobs.jsonl` (every tonyd job), `~/.local/share/tonypi-phone/episodes/` (phone recordings).
Codex native memories stay off (opaque, noisy ASR input); the brain dir is the source of truth.

## Learning
`docs/BODY_LEARNING.md`: MCP `trial`, `compare`, `body_sweep`, `ledger`, `library`, `body_model`, `get_job`;
`tools/learn.py` from the shell. Skills in `.agents/skills/` are procedural memory; a session that works out a
procedure updates its skill.

## Ops runbook
- Status: `systemctl status tonypi-voice tonypi-tonyd tonypi-mcp tonypi-phone`; `journalctl -u tonypi-voice -f`.
- What Tony did: `tony history --today`, `tony goal list`, `tony autonomy status`, `tail
  ~/.local/share/tonypi/live/tasks.jsonl`.
- Quiet please: `tony autonomy pause 120` (or say "pause your own work"). Body only: `tony autonomy body-pause 60`.
- Restart the voice: `sudo systemctl restart tonypi-voice` (L is resumed; tasks are lost). From L itself only via
  `tony_restart_voice`.
- Offline: say "switch to local" (or `sudo systemctl start tonypi-agent`; it stops the voice). Back: "switch to live"
  (or `sudo systemctl start tonypi-voice`).
- Config: `~/.config/tonypi/codex-live/config.toml` = `provision/files/codex-live.config.toml`. Auth is shared from
  Hermes by `tools/share_chatgpt_login.py` (timer `tonypi-chatgpt-share`); Hermes stays installed only as the token
  refresher. Never `codex login/logout` there.
- Roll back a bad self-edit: `git log --oneline` (`live:`, `live-task:`, `auto:` commits), `git revert <sha>`, restart
  the service it touched. Memory edits: `git -C ~/.local/share/tonypi/brain log`.
- Robot tests from Claude Code: hold `flock /run/lock/tonypi-robot.lock` (the learning runner refuses; the autonomy
  scheduler starts no body session and ends a running one within a tick, ~15 s) and `tony autonomy pause` first.
