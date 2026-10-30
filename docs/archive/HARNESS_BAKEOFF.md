# Harness bake-off (2026-10-29)

> Archived 2026-10-30: the bake-off is over. Live mode won; own/Hermes/pi live on branches `deprecated/harness-*`. See `docs/BRAIN.md`.

Goal: pick the brain harness for an always-on Tony that can be told anything ("hey Tony, go to the living room"),
uses every capability as a tool (camera photo/video, body skills, head, speech, sensors), and can be steered mid-task
("actually, turn around"), like a message to Claude Code while it works. Three options are built in parallel, each in
its own worktree branched from `m1-tonyd`, and compared on the same tasks.

| Option | Branch prefix | Brain loop | Robot tools via |
|---|---|---|---|
| 1. Own harness | `harness-own` | our Python loop: Claude Agent SDK (Claude Code login) + local LFM3 fallback | direct tonyd client / MCP |
| 2. Hermes | `harness-hermes` | Hermes Agent as the main engine (64K-context hack, see brief) | `tonypi-mcp` |
| 3. pi | `harness-pi` | pi agent (`--mode rpc` or SDK), steering/follow-up queues | pi extension → tonyd / MCP |

## Shared ground rules (all options)
- `tonyd` (M1) is the body: the only process on `/dev/ttyAMA0` and the camera. Never open the board or `/dev/video0`
  yourself; use `tonypi.tonyd.client.TonydClient` (socket `/run/tonypi/tonyd.sock`) or the MCP server
  (`http://127.0.0.1:8765/mcp`). Hard rules in `CLAUDE.md` still apply (skills only, never raw joint writes; stop and
  fall recovery never depend on an LLM; torque off when idle).
- **One robot test at a time.** Anything that moves the robot, uses the mic/speaker or restarts services runs under
  `flock /run/lock/tonypi-robot.lock <command>` (or holds it in a shell: `exec 9>/run/lock/tonypi-robot.lock; flock 9`).
  Release it when done. Unit tests and fakes need no lock.
- Mic and speaker have one owner: `tonypi-agent` today. For voice tests, hold the lock, `sudo systemctl stop
  tonypi-agent`, run your front end (reuse `tonypi.agent.speech.Listener`/`Speaker`), then start it again.
- `tonypi-llm` (LFM3-Omni-2B, `127.0.0.1:8080`, ~3.9 GB) is shared and there is no RAM for a second copy. Don't
  change its unit or restart it without asking the M1 session first.
- Secrets live in `/home/pi/.config/tonypi/secrets.env` (mode 600, never commit). Logins that need a browser or device
  code (Claude, ChatGPT) are done by the user: print the exact command and ask.
- Keep your work on your own branch; don't merge into `main`.

## Benchmark tasks
Run each as text first (repeatable), then by voice. Robot on the floor, someone nearby.

| # | Task | Measures |
|---|---|---|
| T1 | "wave" | time to first motion, total time |
| T2 | "what do you see?" | snapshot → answer latency, correctness |
| T3 | "look left and tell me if there's a chair" | multi-step perception |
| T4 | "walk forward 3 steps, then turn left" and, 2 s in, "actually, turn right instead" | **steering**: does the change land, how fast |
| T5 | "how's your battery and how hot are you?" | sensors |
| T6 | "find the door and walk toward it" (stop after ~1 min) | open-ended camera loop |
| T7 | T1 + T2 with the cloud brain unreachable | fallback to local LFM3 |
| T8 | "stop" during T6 | instant (no LLM), always |

Record per task: time to first action, total time, success (pass/partial/fail + note), tokens or cost if known.
Per option: RAM/CPU on the Pi at idle and during a task, lines of our own code, setup steps, what broke.

## Report
Write `docs/bakeoff/<option>.md` on your branch (results table + notes + how to run it) and message the M1 session.

## T9: the brain fixes its own turn (added 2026-10-29)
Same prompt for every harness (one command, advanced tier, e.g. gpt-6.1-sol):

> Your left turn keeps tipping you over. Improve turn_left: measure the default, change it in small steps, test,
> and save your best version as turn_left_<suffix>. Don't change the default turn_left.

- Limits: 15 minutes or 40 turn steps, whichever comes first. Hold the robot lock for the whole run. No spotter
  needed: Tony is built to fall (owner, 2026-10-29); the harness handles falls. Tether is on (turns don't count).
- Measure the same way for the default and the final version: 3 trials at count 1 plus 1 trial at count 3, record
  `max_tilt_deg` each. It must still turn: compare before/after snapshots (or heading) and report roughly how many
  degrees each version turns per step; a "fix" that barely turns doesn't count.
- Report: baseline vs final max tilt, degrees per step, falls (and whether the harness recovered by itself), number
  of trials, wall time, what the brain changed (which servos, by how much), and whether it saved a version.
