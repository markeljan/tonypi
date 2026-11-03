# Factory motion audit 1103-t15 (partial)

Continued in [1103-t16](FACTORY_MOTION_AUDIT_1103_T16.md): all 160 distinct source sequences have now been simulated; 77 pass the complete model gate. Physical floor support remains unresolved, so no new hardware result or promotion is claimed.

The task budget ended before the expanded inventory could be simulated or tested on hardware. No new motion was promoted. The everyday profile remains active and no safety code or approval gates were changed.

## Measured results

| Scope | Count | Result |
| --- | ---: | --- |
| Installed imported recordings | 83 | Current limits and stored exact-content simulation evidence audited; 39 model verified |
| Existing composite actions | 18 | Fresh simulation audit: 10 model verified, 8 failed |
| Already available composite actions | 10 | Unchanged; all still marked hardware acceptance pending |
| New hardware trials | 0 | No physical success claimed |
| Newly approved actions | 0 | No eligible matching physical evidence for promotion |

The ten available actions are stand, wave, arms_up, step_in_place, walk_forward, walk_backward, turn_left, turn_right, step_left and step_right. Their availability is model-based and does not establish physical reliability.

The official manufacturer tree contains 116 action files at revision `dc452eaac9991516600860f706f4b889f3305d1d`. The pinned simulator tree contains 118 at `97807f5e26049ce1c3d90b1c80d5d2a92606d40f`, including four custom IK actions. Of the 108 shared filenames, 82 are byte-identical and 26 differ in motion rows. The official source therefore adds 34 named/content variants beyond that simulator tree. These are source-file counts, not a deduplicated total or an approval count.

Sources: [Hiwonder manufacturer actions](https://github.com/Hiwonder/TonyPi/tree/dc452eaac9991516600860f706f4b889f3305d1d/ActionGroups), [pinned simulator repository](https://github.com/pab47/TonyPi/tree/97807f5e26049ce1c3d90b1c80d5d2a92606d40f).

## Physical validation and promotion

The promotion gate requires an unchanged unclipped exact-content simulation and five distinct matching successful hardware trials with current calibration and authoritative job evidence. The existing ledger contains no eligible trial set for current passing candidates. Simulations alone cannot satisfy this gate.

The phone observer was disconnected. Head-camera observations did not establish the robot's feet and floor support, so body trials were not started. Latest readback was 11.023 V, hottest servo 35 C, tilt 2.3 degrees, idle, torque off, no stop latch. Battery was above the cutoff; low voltage was not the reason trials were withheld. Existing get-up composites failed model verification, so automatic recovery availability also needs checking before physical trials.

## Artifacts and continuation

The partial machine audit is stored outside git at `~/.local/share/tonypi/simulation/audits/1103-t15/curated-catalog.json`. Imported manufacturer frames remain outside git.

`tools/audit_factory_motions.py` now provides an explicit model-only all-source audit with provenance, exact-body deduplication, calibration/EEPROM checks, strict simulation-cache matching and resumable metadata. It does not execute or promote hardware actions. The tool was tested with 21 synthetic tests; the existing motion/catalog/promotion/simulation/import/profile regression suite passed 70 tests.

Remaining work: run the expanded audit against the downloaded manufacturer checkout, review all variant results and physical format limitations, then obtain five matching guarded trials per eligible candidate when sensor checks establish suitable support and recovery. Promote only candidates that pass the unchanged gate. Report deduplicated totals after that run; do not present the source-file sum as distinct actions.
