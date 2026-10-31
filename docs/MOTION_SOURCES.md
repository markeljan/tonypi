# Motion sources and verification (2026-10-31)

The everyday catalog is `data/motion_catalog.json`. Its `model_verified` flag means the exact frame sequence
passed this repository's MuJoCo posture check without actuator or calibration clipping. `hardware_verified` is
false for every factory entry: this rework executed **no physical body or head motion**. A completed animation
is not evidence of requested travel, gesture quality, recovery reliability or physical safety.

## Manufacturer assets

[Hiwonder's official TonyPi repository](https://github.com/Hiwonder/TonyPi/tree/dc452eaac9991516600860f706f4b889f3305d1d)
contains 116 SQLite `.d6a` files under `ActionGroups/`. The source checkout was obtained read-only for this audit
at `/tmp/tonypi-motion-audit/Hiwonder-TonyPi` on the Mac. Its recursive tree contains no LICENSE or COPYING;
GitHub reports `license: null`. Consequently manufacturer motion rights are recorded as `NOASSERTION`, not MIT.
The 83 image-derived JSON recordings and all identified frame-derived experiments have been removed from
Git history. Only names, hashes, model results and composition references remain. Import your own original
image/SD actions or official download using [FACTORY_MOTIONS.md](FACTORY_MOTIONS.md). Local import grants no
redistribution rights. The existing Pi library was migrated with all 83 validated frame hashes unchanged.

[Hiwonder's official PC software/action programming documentation](https://wiki.hiwonder.com/projects/TonyPi/en/latest/docs/9.upper_computer_action_editing_course.html)
explains positions 0–1000, keyframe duration 20–9999 ms, stored action groups, composition, repeat and stop.
The documented SDK example stands before repeated forward action groups. The official
[Pro action control course](https://wiki.hiwonder.com/projects/TonyPi_Pro/en/latest/docs/4_pc_software_action_control_courses.html)
also documents `.d6a` group integration. Documentation was read, linked and summarized; it was not copied wholesale.

Sixty existing JSON body-channel sequences exactly match this official checkout. Five differ: `stand_slow`,
`turn_left`, `turn_right`, `bow`, `go_forward`. Eighteen have no matching filename in the current official tree
(including nine dances, power-gait variants and `both_arms_up`/`wings_up`). The manifest records each comparison;
absence from the current tree does not establish the original image's provenance. The owner-adjusted standing posture is preserved only in the local imported actions. Channels 17/18 belong to absent grippers and are explicitly omitted from
body playback/model checks. No channel 19 or arbitrary additional channel is accepted.

## Simulator and other skills

[pab47/TonyPi](https://github.com/pab47/TonyPi/tree/97807f5e26049ce1c3d90b1c80d5d2a92606d40f)
is the already-installed, pinned simulator/action mirror. Its repository
[MIT license](https://github.com/pab47/TonyPi/blob/97807f5e26049ce1c3d90b1c80d5d2a92606d40f/LICENSE)
is copyright 2026 Pranav Bhounsule; the external checkout retains that license. Its model and literal ZERO/SIGN
mapping are used, never its direct-hardware controllers. Manufacturer action provenance is separately recorded
above. See `docs/SIMULATION.md` for modeling uncertainties: this TonyPi Pro model has different calibration,
ideal contact, no battery/heat/backlash and no torque-off behavior.

The Pi's `~/src/bruhOS` at `3fd3ca5acbb7d1dc59ef18adbfedf29ae6068cb6` was inspected read-only,
including README, `configs/tonypi.toml`, LICENSE and `docs/design.zh-CN.md`. It is Apache-2.0 software, with a
short skill JSON whitelist and mocked/kinematic model results. Its README explicitly labels its TonyPi body
**not yet validated on a robot**. Therefore it supplies useful interface precedent, but no verified physical
motor skill or additional motion asset was promoted from it.

## What the checks established

Read-only tonyd calibration returned empty software trim/limits, meaning effective default limits 0–1000.
After an idle-state check, read-only `servo_calibration` returned all 16 EEPROM limits 0–1000 and offsets matching
`robot.yaml`. The EEPROM snapshot is in the manifest. EEPROM offsets already live inside each servo; they are
not added again as software trim. All 83 locally imported assets passed hardware format and calibrated pulse-limit
checks. All 83 were run in the simulator, using prone/supine starts for the two recovery assets. Only 39 passed
both the generic posture check and zero actuator clipping. Failed assets remain available for explicit learning
and investigation; they are excluded from everyday execution, rather than labelled verified.

Composite skills were separately simulated. Repeatable locomotion was tested at every count 1–5. Ten of
eighteen catalog entries pass: `stand`, `wave`, `arms_up`, `step_in_place`, `walk_forward`, `walk_backward`,
`turn_left`, `turn_right`, `step_left`, `step_right`. Everyday forward uses `go_forward_one_step`; turns use
`turn_left_small_step_a` and `turn_right_small_step_a`. These alternatives passed at all five repeat counts;
the old forward/turn paths remain visible as failed individual assets and are not silently substituted in
learning mode. Every count, exact frame hash, source comparison, duration and model result is in the manifest.

`bow`, both kicks, `squat`, `flap_wings`, `lie_down`, `get_up_front`, and `get_up_back` fail a model check and are
unavailable in the everyday catalog. In particular automatic recovery cannot be claimed validated by this
change. The owner's supervised acceptance must resolve model/geometry mismatch or provide a corrected candidate
before these entries can be promoted. No physical trial was attempted to bypass a failed model result.

## Reproduce and promote

On an idle Pi with the pinned simulation dependencies installed:

```sh
uv run python tools/audit_motion_library.py --source-repo /path/to/Hiwonder-TonyPi --output /tmp/motion_catalog.json
```

This command reads tonyd state, calibration and EEPROM parameters, reads factory files, and launches physics
workers. It never requests movement. `--motion-reports` can reuse this audit's report list only when its exact
frames, calibration and simulator adapter still match the saved request. Reports remain under
`~/.local/share/tonypi/simulation/reports/`; manifest report IDs resolve there on the Pi. The manufacturer
checkout must be reviewed separately for revision and license before generating source comparison metadata.

For an owner-requested, authored frame sequence, first simulate its exact frames, then obtain at least five
measured, distinct count-one trials through the learning runner. Promotion is an explicit subsequent operation:

```sh
uv run python tools/promote_motion.py my_skill REPORT_ID --description 'Describe measured behavior and limits'
```

Promotion requires an unchanged successful unclipped upright model report with the current software calibration,
and matching frame candidate hashes in both the ledger and authoritative completed jobs. Every qualifying job
must have the current calibration digest, measured upright start and no detected fall. Legacy jobs without this
provenance cannot qualify. Promotion stores an immutable frame snapshot in `promoted_skills.json`; editing the
original user motion cannot replace it. Only count one is permitted until repeat validation is implemented.
Promoted actions are refused while tethered because arbitrary keyframes do not have reliable displacement/step
accounting. User-written motions and skills without this evidence remain in learning mode; they never override
the curated factory path. Repeating a model pass does not make it physical evidence.
