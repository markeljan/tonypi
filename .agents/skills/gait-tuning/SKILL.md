---
name: gait-tuning
description: 'Make an existing gait or move better (steadier, faster, straighter, turns more): walk, turn, side step, get_up, or any saved move. Baseline, one hypothesis per round, compare, promote with evidence. Also small calibration trims.'
---

# Gait tuning

Only use this procedure after the owner explicitly asks to learn/practice/calibrate, through the separate
`TONYPI_MCP_PROFILE=learning` stdio server (`docs/MOTION_LAYER.md`). Everyday requests use the ten high-level
library tools. A missing motion or body goal alone does not enable fine control; say what is missing instead.
Do not invoke hidden controls through shell/socket. New/changed candidates need calibrated-limit checks,
simulation and the library verification gate before everyday promotion; a save or hardware completion alone
is insufficient. Preserve model evidence separately from measured physical success.

## Review existing options first
Follow [the inventory and evidence review](../../../docs/BODY_LEARNING.md#existing-motions-first) before
creating frames or tuning parameters. Reuse an adequate measured or factory motion first; author/tune only when
the owner has requested learning and an identified shortfall calls for it.

## Baseline
1. `library()` and `ledger(skill=...)`: maybe it's already measured.
2. `trial(skill="<skill>", count=1, trials=3, objective=...)` plus `trial(skill, count=3, trials=1)` from a settled
   stand. Walk: phone in companion mode (`phone-observer`), `objective="walk"`; check `tether()` first, walking spends
   the step budget, turns don't.
3. Note mean and std of the score, yaw per step, max tilt, falls. Known numbers: default `turn_left` ~20 deg/step,
   max tilt ~18 deg; `turn_left_v1_own` (frames x1.4 slower) 23.8 deg/step, tilt 15.8 (gyro, 2026-10-30).

## One hypothesis per round (as gait params)
- Frames too fast, I swing past balance: `{"time_scale": 1.2..1.5}`.
- Too much hip roll (5/13) or ankle roll (1/9) swing: `{"offset": ...}` 10-20 counts nearer the stand pose, or
  `{"amplitude": 0.8..0.9}`.
- The foot lifts before the weight shifted: add or lengthen a lean frame (own frames).
- The step ends away from the stand pose: make the last frame match the first.
- Left/right asymmetry: `{"mirror": true}` of the good side as the candidate for the bad side.
At most ~20-30 counts per servo per round.

## Decide
- `compare(base="<skill>", candidate={"base": "<motion>", "gait": {...}}, trials=5, objective=...)`.
- Promote only on "promote" (> 1 pooled std over >= 5 trials, 0 falls). A turn must keep >= 90 % of the default yaw.
- Long searches (10-30 trials) belong in a background task or autonomy session; from the shell use
  `uv run python tools/learn.py compare ... --trials 10 --budget-s 900`.
- Heading by gyro `yaw_deg`, distance by phone. Never judge a turn or walk from photos.

## Save
- Winner: `save_motion("<skill>_v<N>", frames, "<params>: <numbers> vs <baseline>, n, falls, exp ids")`, then
  `save_skill("<skill>", ["*<skill>_v<N>"], "...")` to make it the default (`delete_skill` restores the factory one).
- Losers stay in the ledger; don't save them as motions.
- `tony goal note`, `tony journal --kind learn`, and one lesson line if it generalises.

## Calibration
Standing crooked (idle upright tilt > 5 deg): `calibration(trim={"<servo>": +-5..10})`, `practice_skill("stand")`,
`get_state`, compare tilt. Soft `limits` keep a servo in range for every motion. `servo_calibrate` (EEPROM) only with
a measured reason and the owner's yes.
