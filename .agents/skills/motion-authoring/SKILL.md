---
name: motion-authoring
description: 'Create a move I don''t have yet (a gesture, jumping jack, high step, a dance, a new gait) by writing keyframes, measuring them with trial, fixing what tips me over, and saving it as a skill. Use only when the owner asks to learn or author a missing move.'
---

# Writing a new motion

Only use this procedure after the owner explicitly asks to learn/practice/calibrate, through the separate
`TONYPI_MCP_PROFILE=learning` stdio server (`docs/MOTION_LAYER.md`). Everyday requests use the ten high-level
library tools. A missing motion or body goal alone does not enable fine control; say what is missing instead.
Do not invoke hidden controls through shell/socket. New/changed candidates need calibrated-limit checks,
simulation and the library verification gate before everyday promotion; a save or hardware completion alone
is insufficient. Preserve model evidence separately from measured physical success.

Loop: draft -> limits and simulation verification -> authorized trial with a guard -> read the numbers (and the phone contact sheet) -> change one thing -> save.
Expect 2-4 rounds. A fall during a trial is information, not a reason to stop.

## Review existing options first
Follow [the inventory and evidence review](../../../docs/BODY_LEARNING.md#existing-motions-first) before
creating frames or tuning parameters. Reuse an adequate measured or factory motion first; author/tune only when
the owner has requested learning and an identified shortfall calls for it.

## 1. Start from something close
`list_skills`, `list_motions`, then `get_motion` of the nearest default (`wave`, `both_arms_up`, `squat`,
`left_move`, `climb_stairs`, `left_shot`...). Joint table, stand pose and mirror rules: `data/motions/AGENTS.md`.
`body_model()` has measured signs and ranges: use them over guesses.

## 2. Draft 3-6 frames
- Start and end at the stand pose, so the next move starts balanced and I can go limp safely.
- Legs (1-5, 9-13) carry me: 300-600 ms per frame, under ~60 counts per frame, left and right balanced.
- Arms (6-8, 14-16) can move fast and far.
- Leg spread: hip roll 5/13 out plus ankle roll 1/9 the opposite way, 15-30 counts. I can't jump: a "jumping jack"
  is arms up while the legs spread, then back.
- High step (rug edge): shift weight onto the standing leg first (hip roll 5/13 and ankle roll 1/9 20-30 counts toward
  it, slowly), then lift the other foot (knee 3/11 +100-150, hip pitch 4/12 +60-100), put it down forward, bring the
  weight back.
- A hold frame needs at least one servo in `pos`.

## 3. Trial it
- `trial(frames=[...], count=1, trials=3, objective="pose")` (guard 30 by default; gaits use objective walk/turn with
  guard 40). Read per trial: max tilt, end tilt, fell.
- Tilt is fused IMU: arm-only moves stay under 10 deg; over 20-25 deg is unstable. Slow the leg frames, shrink leg
  amounts or keep the weight centred.
- To see the shape, use the phone in companion mode (`phone-observer`) and `phone_episode_review`, or the head camera
  looking down (`look(0, -40)` + `video`) for foot moves.
- Change one thing per round. A change that doesn't move the numbers beyond the noise is no change.

## 4. Save and use
After limits/simulation checks and measured hardware comparison meet `docs/BODY_LEARNING.md` criteria:
1. `save_motion("<name>", frames, "<what it is>; max tilt N, n=3, 0 falls")`.
2. `save_skill("<name>", ["<name>"], "...")`; `["*<name>"]` if it should repeat with count.
3. Submit exact content to the library verification gate; only then use it through everyday `run_skill`.
4. `tony journal --kind learn "new motion <name>: ..."`; a general lesson goes to `tony remember --kind lesson`.
Never overwrite a factory default name with an unmeasured move; a better default goes through `compare` first.
