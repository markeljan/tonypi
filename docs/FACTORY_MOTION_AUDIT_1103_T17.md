# Factory motion audit 1103-t17

Continuation of [1103-t16](FACTORY_MOTION_AUDIT_1103_T16.md). Markel explicitly confirmed floor support and authorized physical learning, simulation and promotion. He subsequently relocated Tony and renewed autonomous testing authorization in the new room. No further setup confirmation was requested.

**69 measured trials, 0 recorded falls, 13 new library promotions.** These cover 13 of the 77 previously model-passing exact sequences, represented by 26 source assets. Aliases are not extra independent motions or trials. The other 83 original sequences retain their failed/clipped model status.

## Method and results

Each physical attempt used the existing `simulation_execute` bridge: one count, 30-degree tilt guard, the Lab runner and a closing settle. State and raw joints were checked before trials; simulations were refreshed from all 16 current raw positions whenever the previous initial pose exceeded the 20-count tolerance. A successful, unclipped upright stability simulation, unchanged calibration and five distinct qualifying exact-frame jobs were required by `promote_skill`. Every promotion retains `count_limit=1`, `precondition=standing` and `locomotion_unknown=true`.

Initial preflight measured 11.191 V, hottest servo 37 C, tilt 2.0 degrees, idle, upright, untethered and no stop latch. After 18 successful trials, continuous testing paused at 10.861 V. Following the owner's explicit new-room continuation instruction, fresh readings were 11.269 V, 39 C and 3.4 degrees, upright, idle and no latch. Testing resumed with five seconds of idle time between trials and voltage/temperature checks before and after each attempt. Voltage rebound is not evidence of charging; no charging claim is made.

| Promoted skill | Trials | Peak tilt mean ± sample SD, degrees | Highest peak | Job IDs |
| --- | ---: | ---: | ---: | --- |
| `factory_both_arms_up` | 5 | 3.24 ± 0.36 | 3.6 | 604, 606, 608, 610, 612 |
| `factory_field_goal` | 5 | 3.86 ± 0.35 | 4.2 | 624, 626, 628, 630, 632 |
| `factory_left_hand` | 5 | 3.64 ± 0.47 | 4.2 | 574, 576, 578, 580, 582 |
| `factory_lift_down` | 5 | 2.98 ± 0.48 | 3.7 | 664, 666, 668, 670, 672 |
| `factory_lift_left_hand` | 5 | 2.72 ± 0.48 | 3.5 | 594, 596, 598, 600, 602 |
| `factory_right_hand` | 5 | 3.50 ± 0.00 | 3.5 | 584, 586, 588, 590, 592 |
| `factory_seize_down_right` | 5 | 2.80 ± 0.20 | 3.0 | 644, 646, 648, 650, 652 |
| `factory_seize_right` | 5 | 2.10 ± 0.54 | 3.0 | 654, 656, 658, 660, 662 |
| `factory_stand` | 5 | 2.30 ± 0.00 | 2.3 | 544, 546, 548, 550, 552 |
| `factory_stand_slow` | 5 | 1.48 ± 0.27 | 1.6 | 564, 566, 568, 570, 572 |
| `factory_stand_slow_9ae943fa` | 5 | 1.52 ± 0.70 | 2.7 | 634, 636, 638, 640, 642 |
| `factory_wave` | 5 | 3.70 ± 0.97 | 4.5 | 554, 556, 558, 560, 562 |
| `factory_wing_chun` | 5 | 2.18 ± 0.18 | 2.4 | 614, 616, 618, 620, 622 |

Full score, elapsed-time and gyro-yaw means, sample deviations, frame hashes, report IDs, source aliases and jobs are in the private machine summary. These are balance-completion measurements. Scores across different motion durations do not establish improvement; no incumbent was replaced or claimed better by more than measurement noise. The phone was disconnected: physical distance, foot clearance, object handling, gesture efficacy and repeated gait performance remain unmeasured. Gyro yaw variations near the stated ±3–7-degree noise are not evidence of accurate turning.

## Limits and remaining work

There are 64 original model-passing variants without a new promotion. A model pass is permission to consider a guarded trial, not hardware verification. Count one only was tested; omitted channels 17/18 and source names do not prove head, gripper or object effects.

Testing ended when this task reached its token budget. The controller was stopped during an idle rest after completed settle job 681; no body job was interrupted and no stop latch was created or cleared. This is a partial audit, with no further owner setup confirmation required.

`factory_lift_up` has four successful exact-frame trials (jobs 674, 676, 678, 680) against refreshed simulation report `45d8edb8843948c68fa6a8584bd76714`. It remains unpromoted until a fifth qualifying trial and the existing promotion gate pass. Resume remaining candidates only after fresh sensor, calibration and initial-pose checks.

Both existing get-up actions remain unavailable under their model gates. All three distinct recovery source sequences failed/clipped in t16; no low-level recovery loop or safety bypass was introduced. No safety code, calibration, manufacturer payload in git or approval gate changed.

## Artifacts and verification

- `~/.local/share/tonypi/simulation/audits/1103-t17/events.jsonl`: chronological simulations, physical results, pre/post state and promotion responses (the first stand trial is in the authoritative ledger).
- `~/.local/share/tonypi/simulation/audits/1103-t17/summary.json`: deduplicated job/ledger evidence, frame hashes, statistics, source mappings and holds.
- `~/.local/share/tonypi/promoted_skills.json`: promoted exact frames and five matching jobs per skill, outside git.
- `~/.local/share/tonypi/simulation/reports/<id>/`, `jobs.jsonl`, `brain/learning/ledger.jsonl` and traces retain authoritative source/model/job evidence.

The summary generator asserts exact frame/report/promotion equality, matching learning and job digests, count one, 30-degree guards, completed jobs, no recorded falls and at least five distinct jobs for every promotion. An independent read-only review also verified every promotion predicate for the first seven skills (35 jobs, seven exact variants represented by 14 assets).

Relevant existing factory import/audit, profile, motion catalog/archive/promotion/runtime and simulation tests passed: **135 passed in 4.40 seconds**. Final status confirmed the everyday profile, idle with no job or stop latch, upright at 1.6 degrees, 11.398 V, hottest servo 53 C and torque off. Last settle job 681 completed. A post-movement camera observation was captured at epoch 1793720155.4121454, head revision 7, pan 0 and tilt -40 degrees; the image showed the floor. Final status and observation metadata are saved alongside the summary. Learning clients have exited; all 13 promoted skills are available in the everyday catalog with model and hardware verification. No voice restart is needed.
