---
name: gait-tuning
description: Improve, write or calibrate Tony's own movements (walk, turn, side step, any move) with the studio tools. Use when asked to make a gait steadier or faster, fix a move that wobbles or falls, create a new move, or calibrate servos.
---

# Gait tuning on Tony

You change real keyframes on a 35 cm robot that has fallen during turns today. Work like a careful experimenter:
one small change at a time, measured, and never replace a working default until the new version is shown to be better.

## Before you start

- Say what you are going to try. Tony is built to survive falls: a fall during a trial is data, not a disaster
  (the front end gets him up by itself). He must stand on the floor, not on a table.
- Turns don't use the charger tether's walking budget; walking does (`tether` shows how much is left), so tune
  walks with few steps.
- `get_state` first: upright, battery above ~11.0 V, hottest servo under ~65 C. If not, stop and say why.
- Run every trial with **`guard_tilt_deg: 25`** (run_skill, play_frames, set_pose): the body stops the move if Tony
  tilts past it. Nothing else catches a bad gait: the body has no tilt guard of its own, and balance is your job.
- Stop testing and report back if a guard trips twice, a servo gets above 75 C, or the person says stop. If Tony
  falls, the front end gets him up by itself; wait for that, then `get_state` before going on.

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
   step). `get_motion` the motion and read it: frame times (`ms`), which servos move, how far. Measure the default
   with `guard_tilt_deg: 25`: **3 trials at `count` 1 plus 1 trial at `count` 3**, writing down each max tilt.
   For a turn, also estimate how far it turns per step: `snapshot` before and after a trial and compare how far
   things in view moved (the camera sees about 60 degrees across), or ask the person. A "steadier" turn that
   barely turns is no fix.
2. **One hypothesis.** Common causes of a wobbly or falling step:
   - frames too fast (short `ms`): Tony swings past balance. Try 1.2-1.5x longer frames;
   - too much hip roll (5/13) or ankle roll (1/9) swing: move them 10-20 counts closer to the stand pose;
   - the foot lifts before the weight has shifted: add or lengthen a frame that leans first;
   - the step ends away from the stand pose, so the next step starts off balance: make the last frame
     match the first. A step must end in a pose Tony can hold limp: the body goes limp in whatever pose a job
     ends (after a short hold), and run_skill settles into a stand only when the whole task is done.
3. **Small change, new name.** Copy the frames, change only what the hypothesis says (at most about 20 counts per
   servo, or one timing change), and `save_motion` under a new name with `_pi`, e.g. `turn_left_pi_v2`.
   Make it a skill: `save_skill("turn_left_pi_v2", ["*turn_left_pi_v2"], "...")`. The motion library is shared
   with other brains on this robot, so names you save must contain `_pi`, and defaults are never overwritten.
4. **Test.** Measure the candidate exactly like the baseline (3 trials at `count` 1 plus 1 at `count` 3,
   `guard_tilt_deg: 25`, snapshots for the turn amount) and compare.
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
