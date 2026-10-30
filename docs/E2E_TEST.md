# Final end-to-end test (harness bake-off, 2026-10-29)

Goal: find the best harness for a self-learning Tony with full control of his body and senses. Each harness gets
the same three tasks from the same starting point; the owner times them and judges the method.

## What the harness must be (owner's direction)
- **Full control, every peripheral**: camera (photo, scan, short video/frame burst), head, all body skills, its own
  keyframes/poses, saved motions and skills, calibration, IMU state (`tilt_deg`, `lying`, `heading_deg`, per-job
  `yaw_deg` / `max_tilt_deg`), battery/temps, speaker and mic, the walking tether.
- **Voice through GPT-Live-1** via the shared `tonypi-voice` peripheral (docs/GPT_LIVE.md), with typed input as well. Text-only fallback if
  the live session drops; the local LFM3 stays the offline fallback only.
- **Self-learning**: creates new motions and skills when it lacks one (e.g. a gait, a gesture), keeps what works
  (saved under its own suffix), and gets better over attempts.
- **Overcomes failure, keeps going until success**: a failed step, a fall, a lost target or a refused walk is a
  reason to try another way (get up, turn, re-scan, write a new motion), not to stop. It stops only on success, on
  "stop", or when it has truly run out of options (then it says what it tried and what it needs).
- **Stalls and repeated failures → gather data, then a new idea** (owner, 2026-10-29: a harness kept failing to step
  up onto the rug). After 2 failed tries of the same thing, stop repeating it: collect data (look down at the feet,
  `video` while trying, compare `yaw_deg`/`max_tilt_deg`, `get_joints`), say what's wrong, and try something
  different, outside the box if needed: e.g. author a high-step motion that lifts the foot enough to clear the rug
  edge, approach at another angle, side-step, back up. Save what works as a skill and write the lesson down.
- **Ask the human out loud when stuck**: "I'm stuck at the rug edge, what should I do?" / "What's going on?" via the
  voice service's `ask`, then use the answer. Asking is fine; giving up silently is not.
- **Updates itself on request and by itself**: "learn X", "improve Y", "fix how you do Z" changes its own skills,
  prompts or harness code, and it can do that unasked after a failure. Changes persist and take effect without a
  manual restart where possible (hot reload), and are committed on its branch.
- Falls are the harness's job (tonyd doesn't stand or catch Tony). Tony is built to fall; no spotter needed.

## Task categories (exact tasks are given live; these are examples only)
1. **Walk to a target**: "walk to the chair", "go to the door". Target may be off to the side.
2. **Find something not in view**: "find my shoes", "where's the red cup?". Needs scanning, turning, maybe moving.
3. **New motion**: "do a jumping jack", "wave with both hands", "do a little dance you make up". Needs authoring
   keyframes, testing them on the robot, fixing what tips him over, saving it as a skill.

## Procedure
- Start pose: Tony standing (`settle`), facing the wall, same marked spot, same heading, for every harness and task.
- One harness at a time, holding `flock /run/lock/tonypi-robot.lock` for its whole slot. Stop `tonypi-agent`
  (mic/speaker owner) for the slot and start it again afterwards. Reset the tether budget at the start of each task
  (`tether reset`); it stays on (10 walking steps) while Tony is on the charger cable.
- Before the slot: code frozen and committed; one documented start command; a dry run done.
- Per task, logged by the harness (JSONL) and timed by the owner: command received → first action → done (spoken),
  success (owner judges), tool calls, motions/skills created, falls and recoveries, retries, model/effort used,
  tokens, and a short "how I did it". 10-minute cap per task unless the owner extends it.
- After the three tasks: the owner may ask the harness to improve itself ("learn from that") and re-run one task.

## Readiness checklist (each harness reports these to M1 before the test)
- [ ] GPT-Live-1 voice in/out works on the robot (speak a command, hear the answer); barge-in / "stop" works
- [ ] Typed commands work (same brain)
- [ ] Brain tiers: gpt-6.1-terra (fast) / gpt-6.1-sol (planning, authoring, recovery)
- [ ] Tools: snapshot, scan, video/burst, look, skills, play_frames, set_pose, save_motion/skill, calibration,
      get_state (+yaw/tilt/lying), tether, say; anything missing requested from M1 (tonyd/MCP) or built
- [ ] Task loop: plans, acts, checks (camera + gyro), retries differently, persists until success
- [ ] Fall handling without the model; get up and continue the task
- [ ] Motion authoring loop: draft → test with guard → measure tilt/yaw → fix → save → use
- [ ] Self-update: "learn/improve X" edits its own skills/prompts/code, reloads, commits
- [ ] Logging for timing; one start command; dry run of one task per category
