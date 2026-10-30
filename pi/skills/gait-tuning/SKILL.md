---
name: gait-tuning
description: Improve, write or calibrate Tony's own movements (walk, turn, side step, any move) with the studio tools. Use when asked to make a gait steadier or faster, fix a move that wobbles or falls, create a new move, or calibrate servos.
---

# Gait tuning on Tony

You change real keyframes on a 35 cm robot that has fallen during turns today. Work like a careful experimenter:
one small change at a time, measured, and never replace a working default until the new version is shown to be better.

## Before you start

- Say what you are going to try. **Ask for someone to watch and be ready to catch Tony**, and wait for a yes.
  Tony must stand on the floor, not on a table.
- `get_state` first: upright, battery above ~11.0 V, hottest servo under ~65 C. If not, stop and say why.
- Stop testing and report back if a trial reaches **max tilt above 25 deg**, the body aborts a move, a servo gets
  above 75 C, or the person says stop.

## Tools

`list_skills` (what a skill plays), `get_motion` / `save_motion` / `delete_motion`, `save_skill` / `delete_skill`,
`play_frames` (try keyframes without saving), `set_pose`, `get_joints`, `set_torque`, `calibration`, and
`run_skill` (returns `max tilt`: how far Tony leaned during the job; upright standing is about 0-5 deg).

Servos (positions 0..1000, 500 = centre, about 0.24 deg per count), left/right:
1/9 ankle roll, 2/10 ankle pitch, 3/11 knee, 4/12 hip pitch, 5/13 hip roll, 6/14 elbow, 7/15 shoulder roll,
8/16 shoulder pitch. Left and right legs are mirrored: the same physical motion usually moves a left/right pair in
opposite directions around 500. Check against the default before assuming.

## Workflow

1. **Baseline.** `list_skills` shows what the skill plays (`turn_left = *turn_left`: one motion repeated per
   step). `get_motion` the motion and read it: frame times (`ms`), which servos move, how far. Run the default
   3 times at `count` 1 and write down each max tilt. For a turn, also note how far it turns: take a `snapshot`
   before and after and compare, or ask the person.
2. **One hypothesis.** Common causes of a wobbly or falling step:
   - frames too fast (short `ms`): Tony swings past balance. Try 1.2-1.5x longer frames;
   - too much hip roll (5/13) or ankle roll (1/9) swing: move them 10-20 counts closer to the stand pose;
   - the foot lifts before the weight has shifted: add or lengthen a frame that leans first;
   - the step ends away from the stand pose, so the next step starts off balance: make the last frame
     match the first.
3. **Small change, new name.** Copy the frames, change only what the hypothesis says (at most about 20 counts per
   servo, or one timing change), and `save_motion` under a new name with `_pi`, e.g. `turn_left_pi_v2`.
   Make it a skill: `save_skill("turn_left_pi_v2", ["*turn_left_pi_v2"], "...")`. The motion library is shared
   with other brains on this robot, so names you save must contain `_pi`, and defaults are never overwritten.
4. **Test.** `run_skill("turn_left_pi_v2", 1)` three times, the same as the baseline. Compare the max tilts and the
   turn amount. Then try `count` 2 or 3, still watching the tilt.
5. **Keep or revert.** Better means a lower or equal max tilt on every trial, no aborts, and still a useful turn.
   If it's worse, go back to the previous version and try a different hypothesis. Three to five rounds is plenty.
6. **Recommend, don't replace.** Keep the best version under its `_pi` name and put the evidence in its
   description, e.g. `"v2: frames x1.3 slower; max tilt 6-8 deg vs default 14-19 (3 trials, 2026-10-29)"`.
   The owner promotes a winner to the default name later. Use your version by name (`run_skill("turn_left_pi_v2")`)
   when steadiness matters.

## Calibration

If Tony stands crooked (tilt above ~5 deg when idle and upright), a small `calibration` trim on one servo can
straighten him. Change it 5-10 counts at a time, `run_skill("stand")`, then compare the tilt in `get_state`.
Soft `limits` keep a servo inside a safe range for every motion.

## Report

End with a short spoken summary: what you changed, the numbers before and after, and whether it is adopted.
