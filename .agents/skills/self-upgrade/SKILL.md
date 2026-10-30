---
name: self-upgrade
description: 'Change my own code, prompts, skills or config ("fix how you do X", "add a tool", a bug I found in myself), test it, commit it, load it. Use for any edit in /home/pi/tonypi or the Codex config.'
---

# Upgrading myself

## Pick the smallest fix
1. A procedure: edit or add a skill (`.agents/skills/<name>/SKILL.md`, frontmatter `name` + `description` saying when
   to use it). Loads in new threads at once.
2. A fact or lesson: `tony remember --kind lesson|fact`.
3. Behaviour in general: a prompt (`src/tonypi/voice/prompts/`; read `src/tonypi/voice/AGENTS.md` first). Keep
   `codex_developer.md` <= 6 KB.
4. Code: only when 1-3 can't fix it. Code map in AGENTS.md; read the nested AGENTS.md of the directory first.

## Do it
1. `git -C /home/pi/tonypi status`: note others' uncommitted changes and leave them alone.
2. Read the code path. For a bug, write the failing test first.
3. Change it. Match the style: compact, comments where non-obvious, ~120-char lines.
4. Test: `cd /home/pi/tonypi && uv run pytest -q tests/<relevant>.py` (voice: `tests/test_voice.py
   tests/test_live_speed.py`; brain: `tests/test_brain.py tests/test_autonomy.py`; learning: `tests/test_learn.py`;
   MCP and tonyd: `tests/test_tonyd.py`). Tests use fakes, never the robot.
5. Green -> commit only my files: `git add <files> && git commit -m "live: <what and why>"` (`auto:` from autonomy).
   Red and no fix in sight -> `git checkout -- <my files>` and journal what failed.
6. Load it:
   - skills, MEMORY, goals: nothing to do;
   - `src/tonypi/mcp_server.py` or `src/tonypi/learn/`: `sudo systemctl restart tonypi-mcp`, then call one tool;
   - voice code, prompts, Codex config: `tony_restart_voice(reason)` from L, only when the owner isn't talking. A task
     or autonomy session sets `needs_voice_restart` / journals it instead;
   - `src/tonypi/brain/`: the `tony` CLI uses it at once; the voice loads it on restart.
7. `tony journal --kind code "<commit>: <what>"`.

## Never without the owner's yes
Safety code (`src/tonypi/tonyd/`, the voice stop reflex), `tonypi-llm`, auth (`codex login/logout`,
`tools/share_chatgpt_login.py` refresh), network, boot, the watchdog, earlyoom, bootcheck. Never print secrets.

## Codex config
`~/.config/tonypi/codex-live/config.toml` and `provision/files/codex-live.config.toml` stay identical. Check keys
with a scratch copy: `CODEX_HOME=$(mktemp -d)`, copy the config there, `codex features list`, `codex debug
prompt-input`. Never in the live CODEX_HOME.
