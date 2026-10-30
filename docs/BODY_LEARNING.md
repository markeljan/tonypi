# Body learning (2026-10-30)

How Tony learns and improves movement. Replaces the procedure in `docs/BODY_EXPERIMENTS.md` (kept as history). Code:
`src/tonypi/learn/` (runner, gait transforms, metrics, phone client). Skills: `body-learning`, `gait-tuning`,
`motion-authoring`, `phone-observer`.

## Principles
- **Measure, don't judge.** Every candidate is scored from numbers: tonyd's job record (max tilt, gyro yaw, end tilt,
  lying), fresh state, and the phone's tracked path. Never from photos or by asking someone.
- **Code runs the trials, the model designs them.** One `trial` call runs a whole batch: preflight, settle, guarded
  move, recording, scoring, ledger. The model writes candidates and objectives and reads summaries.
- **Falls are data.** Tony lives on the floor and is built to fall. A fall is logged, `get_up` runs (twice at most,
  then the batch stops), and the next trial starts. Guards (`guard_tilt_deg`: gaits 40, balance and new poses 30) end
  a bad trial early.
- **Real limits:** stop and its latch, battery >= 10.9 V, hottest servo < 70 C (tonyd skips at 81, stops at 83), the
  robot lock (Claude Code or tests using the robot).
- Today's lesson (2026-10-30): +-10-count joint jiggles, one LLM turn each, produced nothing. Sweeps of +-150 counts
  and batches of scored trials do.

## Existing motions first
Before authoring or tuning, and when choosing an action for a task:
1. Define the actual outcome: showing an action, turning a measured angle, crossing an edge, or holding balance.
   Do not silently turn a request to demonstrate a built-in action into a development project.
2. Inventory `list_skills()` and `list_motions()` (local/factory), plus `simulation_catalog()` for supported
   PAB47/TonyPi actions. Inspect the nearest candidate with `get_motion`; check catalog support reasons before
   assuming an upstream action is executable. Use the `tonypi-simulation` skill for imported actions.
3. Review `library()`, `ledger(skill=...)`, relevant `get_job` records and `tony recall <task or motion>`.
   Match exact variant, count, starting pose, surface/obstacle and objective; include failures and recent
   contradictory results. Rank comparable measured task success first (factory or custom), then suitable factory
   actions with limited evidence, then unmeasured modified/custom candidates. Do not treat a factory label as a reliability rating.
4. Use an adequate existing option first. With body authorization, check state and surroundings using sensors,
   then test only relevant candidates with a guard (gaits 40 degrees, balance/new poses 30), stable finish and
   observation suited to the outcome. No exhaustive action sweep for an unrelated task. For a demonstration,
   one guarded execution may answer the request; for reliability, use 3–5 measured trials. Simulation requested
   or needed for a candidate comes before its authorized hardware test and remains model-only evidence.
5. Author or tune only after identifying the missing capability or measured shortfall, or when explicitly asked
   for a new action. Reuse stable segments of the nearest action and state the hypothesis before the next batch.

Report evidence at its actual level: **available**, **simulation passed**, **hardware action completed**, or
**measured task success** (criteria, conditions, successes/trials, falls and job/experiment IDs). A done job and
upright finish do not prove stair ascent, foot clearance, rug crossing, distance or repeatable reliability.
Use gyro for heading and validated phone tracking for distance; post-move images can support obstacle/foot
observations but cannot establish distance. Missing or conflicting evidence stays unverified.

### Stair evidence, 2026-10-30
The requested built-in `climb_stairs` demonstration is associated with job 327 (`_play`, 13 frames, 8.3 s inputs):
done, peak tilt 24.8 degrees, end tilt 3.5 degrees, lying false. This establishes one upright completion, not
reliable stair climbing or rug crossing. Task 1030-t2's journal records **a modified half-amplitude stair
candidate**, not the identical factory action, falling 3/3 (jobs 154/157/160) at the later weight transfer.
Keep those variants distinct; neither result erases the other. Rug crossing remains unverified.

## Tools
| MCP (`tonypi`) | Shell (`uv run python tools/learn.py ...`) | What |
|---|---|---|
| `trial(skill \| frames \| base+gait, count, trials, objective, guard_tilt_deg, label)` | `trial --skill X ...` | measured repeats of one move |
| `compare(base, candidate, trials, objective)` | `compare BASE CANDIDATE --trials N` | interleaved A/B with a verdict |
| `body_sweep(joints, lying)` | `sweep [--joints ..] [--standing]` | joint map into body_model.json |
| `ledger(skill, label, limit)` | `ledger` | trial records |
| `library()` / `body_model()` | `library` / `body-model` | what was learned |
| `get_job(id)` | | any finished tonyd job (also `~/.local/share/tonypi/jobs.jsonl`) |
| | `gait BASE --gait JSON` | print a candidate's frames, no robot |
MCP calls return within ~200 s (rerun for more); the shell takes `--budget-s` for long batches in tasks and autonomy.

## One trial
1. Preflight: tonyd idle, no latch, battery >= 10.9 V, hottest < 70 C; lying → `get_up` first (not with
   `start="as_is"` or a `setup`).
2. Settle to a stand (or run the `setup` skill unmeasured, e.g. `lie_down` before a measured `get_up`); fresh state.
3. Phone recording if the phone is in companion mode (label `exp-<id>`).
4. The move with its guard; wait; 1.5 s tail; fresh state; stop recording.
5. Score (below); one ledger line: exp id, label, skill/candidate hash, params, metrics, score, job id, episode id.
Before every move the runner checks the autonomy preempt mark (`tonypi.brain.preempt_file()`, written when the voice
service ends a session): a batch started before it stops. A cancelled move (stop, or another brain moved) or a preempt
ends the batch without the closing settle / get_up, so it never overrides the move that took the body.

## Objectives (`learn/metrics.py`; compare scores only within one objective and basis)
| Objective | Score (higher is better) | Needs |
|---|---|---|
| walk | forward m/s − 0.02·abs(yaw) − 0.5·[max tilt > 25] − 2·fell | phone (else 5 cm per completed step, ranking only) |
| turn | abs(yaw)/step − 3·displacement m − 0.1 per tilt deg over 20 − 2·fell | gyro |
| balance | seconds held − 0.05·max tilt − 2·fell | IMU |
| pose | −end tilt/10 − 1·[not done] − 2·fell | IMU |
| recover | −end tilt/10 − 0.05·seconds − 1·[not done] − 2·fell (no default guard) | IMU; `setup="lie_down"` |
| generic | −max tilt/10 − 1·[not done] − 2·fell | IMU |
fell = lying or end tilt > 45 deg.

## Candidates and search
- A candidate is a base motion plus a few parameters (`learn/gait.py`): `time_scale` (>1 slower), `amplitude`
  (scale around the stand pose), `offset` {servo: delta}, `mirror` (left ↔ right), `repeat`. Own keyframes when
  no base fits (`motion-authoring`).
- One hypothesis per round, 3-4 candidates, the best repeated. Noise: yaw +-3-7 deg/step, phone distance +-2 cm,
  tilt a few degrees. A difference inside the noise is no difference.
- A turn must keep >= 90 % of the default's yaw; a steadier move that does less is not a fix.
- **Promote** when `compare` says "promote": candidate mean better by > 1 pooled std over >= 5 trials with 0 falls.
  Save with `save_motion` (evidence in the description) and `save_skill` under the skill's name; `delete_skill`
  restores the factory default. Record the result in the goal notes, the journal, and a lesson if it transfers.

## Curriculum (`~/.local/share/tonypi/brain/curriculum.json`)
0. Joint map (lying for legs, standing for arms): sign, range, speed, error for all 16 servos.
1. Weight shift left/right: largest ankle/hip-roll shift held 3 s inside a 30 deg guard.
2. Single-leg stand: 3 s per leg; knee lift judged on phone frames.
3. Step in place: 10 steps, drift < 5 cm.
4. Turn: 90 deg +-10 each way by gyro, no fall in 10.
5. Walk forward 1 m: phone-measured, lateral drift < 15 cm, 3/3 without falling.
6. Side step, walk backward.
7. Get-up reliability: >= 9/10 front and back.
8. Composite: step over the rug edge; walk to an object found by `phone_find`.
A rung unlocks when its prerequisites are done; the autonomy scheduler makes the next unmet rung a self goal.

## The phone as observer (skill `phone-observer`)
- Placement Tony asks for once: on a stand, plugged in, ~1.5 m away at hip height, whole body plus ~1 m of floor in
  front in view; side-on for walks and leg lifts, facing for turns and balance. Then he verifies with
  `phone_snapshot` + `phone_track` and asks nothing else.
- Reliable: distance walked (`target_moved_m`, path; noise 1-2 cm, trust >= 10 cm), falls, the shape of a move.
- Not reliable: Tony's heading (use gyro yaw), joint angles, foot clearance under ~1 cm (judged, stored as judged),
  displacement while the arms swing. `phone_pose`/`phone_motion` are the phone's own in companion mode.
- No `phone_say` in companion mode: the phone mic hears it and it comes back as a command.

## Known numbers
- Default `turn_left` ~20 deg/step, max tilt ~18 (gyro re-measure 2026-10-30); `turn_left_v1_own` (frames x1.4
  slower) 23.8 deg/step, tilt 15.8: the best turn measured so far, not yet promoted through `compare`.
- Tilt under 10 deg is steady; over 20-25 is unstable. Leg spread over 40 counts was unstable.
- About 150 trials an hour are possible (a 4-step walk trial 10-15 s, a fall plus get_up about +15 s).
