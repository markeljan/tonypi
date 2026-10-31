---
name: body-learning
description: 'Learn or improve any body skill by measured trials (trial, compare, body_sweep, ledger, library). Use for "learn to X", "get better at X", body goals in goals.json, mapping joints, or judging whether a move works. Not for a single canned move on request (just run_skill).'
---

# Body learning

Only use this procedure after the owner explicitly asks to learn/practice/calibrate, through the separate
`TONYPI_MCP_PROFILE=learning` stdio server (`docs/MOTION_LAYER.md`). Everyday requests use the ten high-level
library tools. A missing motion or body goal alone does not enable fine control; say what is missing instead.
Do not invoke hidden controls through shell/socket. New/changed candidates need calibrated-limit checks,
simulation and the library verification gate before everyday promotion; a save or hardware completion alone
is insufficient. Preserve model evidence separately from measured physical success.

I learn like an engineer: a parameterised candidate, a measured objective, repeats, a verdict. Code runs the trials;
I design candidates and read the numbers. Falls are data. Full method: `docs/BODY_LEARNING.md`.

## Existing motions first
Before authoring or tuning, follow the inventory and evidence review in
[docs/BODY_LEARNING.md](../../../docs/BODY_LEARNING.md#existing-motions-first). Prefer an existing motion that
already meets the task in comparable conditions; a factory name alone is not proof. A direct request to show a
supported action needs a relevant guarded execution, not a tuning project.

## Before
1. `get_state`: battery >= 10.9 V, hottest servo < 70 C, no stop latch. Lying is fine: the runner gets up first.
2. `body_model()` and `library()`: what I already know. `ledger(skill=...)` for past trials of this skill.
3. Phone: `phone_status`. If connected, follow the `phone-observer` skill (companion mode, tracking) for any walk or
   displacement objective. Without a phone, walk scores fall back to step counts; turns and balance need no phone.

## Map the body first (curriculum rung joint_map)
- `body_sweep(lying=True)`: lies down, sweeps the 10 leg servos +-150 counts in 3 s ramps, records sign, range,
  error, speed, then gets up. `body_sweep(lying=False)`: the 6 arm servos while standing.
- It stops at ~200 s: rerun with the joints it lists as not swept. Check `body_model()` covers all 16.

## Measure a skill
- Baseline the incumbent: `trial(skill="turn_left", count=2, trials=5, objective="turn")`.
- Objectives: walk (forward m/s from the phone), turn (|yaw| per step, displacement penalty), balance (seconds held,
  max tilt), pose (end tilt), recover (getting up: end tilt, time, still lying = fail), generic. Guards default to 40
  (walk, turn), none (recover) and 30 (others).
- Trials start standing (get_up if lying, then settle). To measure from somewhere else: `start="as_is"`, or a
  `setup` skill run unmeasured before every trial. Getting up from the back:
  `trial(skill="get_up", setup="lie_down", objective="recover", trials=5)` (key `get_up@lie_down`); from the front,
  save a setup skill that ends face-down and use it the same way. Without a setup, a get_up trial only measures
  standing up from a stand.
- A candidate is a base motion plus gait params, not hand-edited frames:
  `trial(base="turn_left", gait={"time_scale": 1.3}, count=2, trials=5, objective="turn")`.
  Params: `time_scale` (>1 slower), `amplitude` (scale around the stand pose), `offset` {servo: delta},
  `mirror`, `repeat`. Own frames: `trial(frames=[...], count=1, trials=3)`.
- Decide with `compare(base="turn_left", candidate={"base": "turn_left", "gait": {"time_scale": 1.3}}, trials=5,
  objective="turn")`. Verdict "promote" = mean better by > 1 pooled std over >= 5 trials with 0 falls.

## Search
- One hypothesis per round (see `gait-tuning`). 3-4 candidates per round, the best one repeated.
- Noise: yaw +-3-7 deg per step, phone distance +-2 cm. A difference smaller than the noise is no difference.
- A turn must keep >= 90 % of the default's yaw. A steadier move that does less is not a fix.
- Two falls in a row on one candidate: drop it, try something genuinely different.

## Promote and record
1. "promote": `save_motion(name, frames, description)` with the evidence in the description ("time_scale 1.3: 23.8
   deg/step vs 20.4, tilt 15.8 vs 18.0, n=5, 0 falls, exp ids ..."), then `save_skill` under the skill's name or a new
   one. Get the frames with `uv run python tools/learn.py gait <base> --gait '<json>'`.
2. `tony goal note <id> "<numbers>"`; done only when the goal's success criteria are met by measurements.
3. `tony remember --kind lesson "<one general line>"` only for lessons that transfer ("slower frames cut turn tilt").
4. Update the matching skill text when a procedure changed. Commit skill edits (`live:` or `auto:`).

## Rules
- Every move with a guard. Never ask anyone to watch, hold, clear or judge: the numbers judge.
- Heading from the gyro (`yaw_deg`), distance from the phone, never from photos.
- tonyd refuses while latched (owner said stop): stop the work, note it, do non-body work.
- `trial` refuses while the robot lock is held (Claude Code or tests): do non-body work and come back.
- A batch stops by itself when its move is cancelled or the session is preempted (the owner spoke), without a closing
  settle: `stopped` says so. Don't restart it in the same session.
