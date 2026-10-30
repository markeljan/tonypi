---
name: tune-motion
description: Write or improve a body move (a gait like turn_left or walk_forward, or a new gesture) from keyframes, measure it, and save it as a user motion/skill.
version: 1.0.0
metadata:
  hermes:
    tags: [robot, motion, gait, calibration]
    category: robot
---

# Tune a motion

Use when asked to fix, improve or invent a move ("your turns keep tipping you over", "learn to wave with both
arms"), or after a move failed or `max_tilt_deg` was high. Someone should be watching the first tries; say what you
are about to test. Only run test moves when the owner has said someone is next to Tony.

Joint map (servo id: L/R): 1/9 ankle roll, 2/10 ankle pitch, 3/11 knee, 4/12 hip pitch, 5/13 hip roll, 6/14 elbow,
7/15 shoulder roll, 8/16 shoulder pitch. Positions 0-1000, 500 = centre, ≈0.24° per count. Frames:
`{"ms": 400, "pos": {"4": 620, "12": 380}}`; servos left out keep their position.

1. Start from what exists: `list_skills` (which motions a skill plays), `get_motion(name)` of the default.
   Baseline: `run_skill(name, 1, guard_tilt_deg=40)` and note `max_tilt_deg` and whether it worked (snapshot / get_state).
2. Change one thing at a time, in small steps (≤30 counts per servo, or slower `ms`). Typical fixes for a wobbly
   gait: slower frames (+20-50 % ms), smaller hip-roll (5/13) and ankle-roll (1/9) swings, feet back under the body
   (a neutral stand frame) at the end of each step.
3. Try it without saving: `play_frames(frames, guard_tilt_deg=40)` once (the guard stops it before a topple). Compare `max_tilt_deg` with the baseline; a stable stand is
   < 10°, a normal step < 20°. Over 30° or a fall: undo that change. tonyd guards temperature only; tilt is your guard_tilt_deg, and the body
   keeper gets Tony up if he falls (wait for it, then check get_state).
4. Keep the better version under a name ending in `_hermes`, never a default name (the user layer is shared with
   the other brains; overriding "turn_left" would change their turns too):
   `save_motion("turn_left_step_hermes", frames, "why it is better")`, then
   `save_skill("turn_left_hermes", ["*turn_left_step_hermes"], "slower, narrower turn")`. Test with count 1, then 2,
   and use `turn_left_hermes` from then on. The owner promotes a winner to the default name later.
5. Report the before/after numbers in one or two sentences. Save a short note to memory ("use turn_left_hermes:
   max tilt 25° → 12°"). `delete_skill` / `delete_motion` remove your versions; never delete a default's name.
6. Calibration (`calibration(trim=...)`) is for a servo that is consistently off at rest (e.g. the stand pose leans):
   small trims (≤20 counts), check the stand with `get_state` tilt. `servo_calibrate` (the servo's own EEPROM) only
   to fix a clearly bad calibration, with the reason; never as part of gait tuning.
