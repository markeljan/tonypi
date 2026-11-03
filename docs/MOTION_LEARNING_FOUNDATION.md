# Motion learning foundation audit — task 1103-t21

Audit date: 2026-11-03. No physical trials or robot calibration changes were made in this task.
This is an audit of capabilities and evidence, not verification of push-ups or single-leg balance.

## What is available

- `src/tonypi/config/robot.yaml` names all 16 body servos. Left/right knees are 3/11; elbows are 6/14.
  Left body servos are 1–8, right are 9–16. EEPROM offsets, runtime trim, raw feedback and motion-space
  commands are different quantities; raw imported action pulses must go through the existing adapter.
- The local `learning/body_model.json` contains all 16 joints, but each has only **one** sweep (`n=1`).
  Its `sign=+1` records encoder following, not anatomical flexion. `range_seen` is an observed loaded
  range, not a mechanical or safe limit. `speed_cps` is response during an imposed ramp, not maximum
  servo speed. Hip rolls 5/13 had endpoint errors of **94/97 counts**.
- `tools/body_feedback.py` captures state, raw/motion-space joints, calibration and target error without
  moving. `tools/body_inventory.py` also reports motion command extrema; extrema are not measured limits.
- `src/tonypi/learn/gait.py` transforms existing frames by timing, amplitude, offset, mirror, repeat and
  concatenation. It silently clamps positions/timings; inspect the resulting content before simulation.
- The separate learning MCP profile exposes authoring, simulation and guarded trial tools. The everyday
  profile retains ten library tools. Profile separation and owner learning authorization still apply.
- `Lab` records trial outcomes, IMU tilt/yaw, duration, temperature change, job/episode ids and optional
  trace references in `learning/ledger.jsonl`; `compare` interleaves candidates. Phone measurements are
  required for displacement claims. A completed action does not prove the intended contact or task.
- `src/tonypi/learn/balance.py` already groups exact recipes/conditions and accepts explicit phone contact
  review with observed hold duration. It does not infer one-leg support from IMU or whole-job duration.
  Five successful observations alone still give an approximately **45%** one-sided 95% upper bound on
  the failure probability; they are a small sample, not a reliability guarantee.
- MuJoCo integration reads 118 pinned PAB47 `.d6a` actions, local motions and static composite skills.
  Native trim/limits, clips, source revision and exact simulation requests are retained. The shell
  simulator has no hardware command. `tools/sim_action_probe.py` replays saved requests with phase
  contacts, sole geometry, COM and actuator-force instrumentation, explicitly as model-only data.
  See [SIMULATION.md](SIMULATION.md) for limits, prior validation and the separate execution gate.

## Evidence gaps identified

1. The prior comparison rule recommended promotion for task 1103-t19's five-pair right-turn result:
   **5.34 ± 1.365 degrees** versus **3.06 ± 0.611 degrees**, a **2.28-degree** gain. This is below the
   documented **3–7-degree** gyro noise. Absolute angle also does not establish the requested direction
   or speed. Comparisons need explicit noise floors, direction/rate metrics, completed finite telemetry,
   matched conditions and exact content/calibration provenance.
2. `src/tonypi/motion/promotion.py` selects successful matching rows before counting them. Five successes
   plus another same-content failed attempt can therefore hide the failure. Promotion should audit and
   report the complete relevant cohort, with explicit conditions, rather than cherry-pick successes.
3. No reusable phase composer/exporter was found. A useful next tool would select source frame ranges,
   retain original timings, add explicit entry/return transitions and holds, reject silent clipping,
   and export exact frame hashes plus phase metadata for model and sensor review. It should report
   commanded rates and observed sweep coverage without presenting either as a safety certificate.
4. The present mapping is useful for reuse, but anatomical signs, repeatability, tracking under load,
   mechanical limits and maximum speeds remain unmeasured. Do not infer them from one sweep or from
   the simulator's different robot.
5. Generic upright tilt checks and the simulation execution gate cannot establish push-up depth,
   hand support or single-leg hold duration. Low postures also interact with lying detection and tilt
   guards. Any changes to daemon safety behavior require Markel's approval; do not raise guards or
   bypass the gate to force these actions through.
6. The sweep return path currently submits its return command before checking whether the outward
   ramp was cancelled, and omits the standing sweep guard on that return (`runner.py`, `_sweep_joint`).
   Fix and regression-test cancellation handling before using new sweeps for calibration evidence.

## Official source comparison

The read-only upstream review pinned [Hiwonder/TonyPi](https://github.com/Hiwonder/TonyPi/tree/dc452eaac9991516600860f706f4b889f3305d1d/ActionGroups)
at `dc452eaac9991516600860f706f4b889f3305d1d` (2026-01-19): **116** `.d6a` recordings.
The installed [PAB47 revision](https://github.com/pab47/TonyPi/tree/97807f5e26049ce1c3d90b1c80d5d2a92606d40f)
has **118**. Of 108 overlapping filenames, **82** Git blobs match and **26** differ. A matching name
therefore does not establish matching keyframes or timing.

The official [app dictionary](https://github.com/Hiwonder/TonyPi/blob/dc452eaac9991516600860f706f4b889f3305d1d/ActionGroupDict.py)
advertises `push_ups`, but neither inspected checkout contains `push_ups.d6a`. No authoritative push-up
keyframes or timings were obtained. The official download/image contents remain uninspected; absence
from these trees does not establish that Hiwonder never supplied a recording. `sit_ups` is a different action.

PAB supplies `tonypi_stand_left/right`: each has **78 frames / 16,040 ms**, including a 4,000 ms entry,
with matching initial/final targets. Its `control/controller_stand.py` generates ramp, bilateral settle,
weight shift/lift, hold, foot-first return and settle phases. These are useful existing model candidates,
not measured single-leg success on Tony.

The official [player](https://github.com/Hiwonder/TonyPi/blob/dc452eaac9991516600860f706f4b889f3305d1d/HiwonderSDK/hiwonder/ActionGroupControl.py)
uses each SQLite row's `Time` as movement duration and wait. Preserve gait transitions: official
`go_forward_start`, middle and end are respectively **9/1,075 ms**, **10/750 ms**, and **2/175 ms**;
PAB's middle/end instead total **950 ms** and **3/800 ms**. The
[Hiwonder editor documentation](https://docs.hiwonder.com/projects/TonyPi_Pro/en/latest/docs/4_pc_software_action_control_courses.html)
uses 20–9,999 ms frames, duplicate poses for holds and matched initial/final poses. Its integration feature
appends recordings; it does not calculate blended transitions. Tutorial bow frames differ from the
shipped asset, so inspect recording bytes rather than treating tutorial examples as exact specifications.

The official runtime repository has no detected license; PAB's MIT license does not settle manufacturer
recording redistribution rights. [Official resources](https://docs.hiwonder.com/projects/TonyPi_Pro/en/latest/docs/resources_download.html)
are a remaining place to locate the missing push-up recording. Keep recordings outside Git.

## Next measured work

Fix full-cohort promotion review and sweep cancellation handling, and build the strict phase composer
before adding new physical actions. Reuse verified manufacturer/local phases where suitable, simulate the
exact candidate and starting pose, and retain request/report hashes. Then use a separately authorized
learning session for 3–5 guarded trials per candidate, with real phone contact review and task-specific
measurements. The phone was disconnected at this audit; no physical success is established here.

Do not commit manufacturer frame tables or locally derived candidates. Keep them in the local data store;
only provenance, aggregate evidence and tooling belong in this repository.

## Change and verification in this task

The comparison gap above is addressed in `learn/metrics.py` and `learn/runner.py`:

- A gain must exceed both pooled standard deviation and the conservative **7-degree** gyro noise floor
  (converted to the rate score when appropriate), with at least five completed trials on each side.
- Missing/non-finite telemetry, falls, incomplete batches, unknown content/calibration and mismatched
  objective, basis, count, start, setup, hold, guard or surface produce explicit ineligibility reasons.
  Recording a trial still works when optional provenance is unavailable; that evidence is ranking-only.
- Turn specs accept `turn_direction: left|right` (inferred from `turn_left*`/`turn_right*`) and
  `turn_metric: angle|rate`. Angle remains the default. Wrong-way yaw cannot qualify, and turn comparisons
  retain at least **90%** of baseline absolute yaw before a rate improvement can be recommended.
- Rows now retain comparison conditions, calibration digest and content fingerprint. Historical candidate
  keys/general library aggregation are unchanged; the separate promotion-cohort issue remains outstanding.
- A comparison recommendation does not verify a library action or prove single-leg support. Balance
  ranking still uses whole-action time; use explicit observed contact/hold evidence for the task outcome.

Regression coverage includes the recorded **2.28-degree** gain, invalid telemetry, mismatched conditions,
wrong direction, changed/unavailable provenance and a faster turn retaining only half the baseline angle.
Independent diff review found and resolved the yaw-retention case. Verification:

```sh
uv run pytest -q tests/test_learn.py tests/test_learn_record_duration.py tests/test_motion_promotion.py
# 79 passed in 1.30s; fake robot clients, no physical motion
git diff --check
# clean
```

The task reached its configured token budget. The strict phase composer, promotion cohort fix, sweep
cancellation fix, official download inventory and further measured calibration remain unfinished.
