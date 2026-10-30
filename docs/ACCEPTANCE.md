# Acceptance checks for Tony's mind (2026-10-30)

How the owner (or Claude Code) checks that the live-mode brain works. Each check is measurable from logs; none needs
anyone to watch the robot. Replaces `docs/archive/E2E_TEST.md`. Robot checks run with Tony on the floor, under
`flock /run/lock/tonypi-robot.lock` only when Claude Code drives them (then `tony autonomy pause` first).

| # | Check | Pass | How to measure |
|---|---|---|---|
| 1 | Fast first motion | "Tony, wave" → first motion < 8 s after the owner stops speaking | `conversation.jsonl` user line `t` vs the wave job's `t_start` in `~/.local/share/tonypi/jobs.jsonl` |
| 2 | Requests reach the brain once | >= 9 of 10 spoken commands reach L exactly once (no miss, no duplicate turn) | 10 varied commands; count delegations/bridges per utterance in `conversation.jsonl` and `journalctl -u tonypi-voice` |
| 3 | Results spoken once | 0 results spoken twice over the 10 commands and 2 background tasks | `conversation.jsonl` assistant lines |
| 4 | No setup questions | 0 questions about setup, clearance, observers, fixtures or tables in a 1 h session with body learning | `grep -iE "clear|observ|fixture|table|nearby|support" conversation.jsonl tasks.jsonl` and the journal |
| 5 | Autonomy starts | a session starts within 2 min of quiet (75 s idle + one 15 s tick) | journal `[autonomy]` start line vs the last `[talk]` line |
| 6 | Owner preempts autonomy | owner speech ends a running session and stops the body < 2 s | journal end line "owner spoke" and the job's `t_end` vs the speech time |
| 7 | Body model | after one unattended session on goal g1, `body_model()` has all 16 servos with sign and range | `tony goal show g1`, `uv run python tools/learn.py body-model` |
| 8 | Ledger rows | every trial has a ledger line with job id; phone trials have an episode id with tracked frames > 0 | `uv run python tools/learn.py ledger --limit 50` |
| 9 | Goals and journal move | after a day: goal notes on worked goals, journal lines of kinds talk, task, autonomy, learn | `tony goal list`, `tony history --today` |
| 10 | Falls are cheap | a fall during a trial is followed by get_up and the next trial without a question | ledger `fell: true` rows followed by more rows in the same batch |
| 11 | Stop never waits | "stop" halts motion < 0.5 s, latched until the owner speaks; no autonomy body work for 30 min | tonyd job `t_end` vs speech time; `tony autonomy status` |
| 12 | Memory | a fact the owner states is in MEMORY.md and recalled in a new thread | say "my favourite colour is green"; restart the voice; ask |
| 13 | Identity | asked "who are you", Tony answers as a robot that learns and upgrades himself; never "I'm ChatGPT" or "I can't move" | `conversation.jsonl` |
| 14 | L stays fast | median L turn < 6 s, context compacts before 80k tokens | L rollout in `~/.config/tonypi/codex-live/sessions/` |

Offline unit tests (no robot): `uv run pytest -q tests/test_voice.py tests/test_live_speed.py tests/test_brain.py
tests/test_autonomy.py tests/test_learn.py tests/test_tonyd.py`.
