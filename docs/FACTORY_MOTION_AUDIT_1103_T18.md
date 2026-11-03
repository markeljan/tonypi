# Factory motion audit 1103-t18

Continuation of [1103-t17](FACTORY_MOTION_AUDIT_1103_T17.md), with Markel's explicit authorization to continue guarded learning and promote qualifying factory motions in the new room.

**101 additional measured trials, zero recorded falls, 21 new promotions: 34 approved exact variants in total.** The combined t17/t18 evidence comprises 170 trials. There are **43 of the original 77 model-passing variants still unapproved**: one failed a fresh model check and 42 were not physically attempted during this continuation. The other 83 originally failed/clipped variants retain their holds. This is a partial result; the session reached its work budget before the queue was complete.

## Method and results

Each physical attempt used the published learning MCP simulation bridge, count one and a 30-degree tilt guard, followed by the existing closing settle. Preflight checked idle state, upright posture, no latch, battery at least 10.9 V, hottest servo below 70 C and unchanged calibration. Current raw joints had to match the simulation's starting pose within the existing 20-count tolerance; simulations were refreshed when needed. State was sampled before and after trials, with an additional everyday-status monitor during the later portion of the run. All observed trial-boundary readings met the limits: battery 11.301–11.868 V and hottest servo 41–59 C. These are sampled readings, not a claim of continuous voltage measurement.

Promotion used the existing gate without changes: successful unclipped model verification, exact frame and calibration identity, and five distinct qualifying completed physical jobs. Every saved promotion retains `count_limit=1`, `precondition=standing` and `locomotion_unknown=true`. `factory_lift_up` combined its four t17 trials with its fifth trial here; every other newly promoted motion received five trials here.

| Newly promoted skill | Qualifying trials | Peak tilt mean ± sample SD, degrees | Job IDs |
| --- | ---: | ---: | --- |
| `factory_0` | 5 | 3.82 ± 1.12 | 784, 786, 788, 790, 792 |
| `factory_catch_ball_turn_left` | 5 | 5.10 ± 0.67 | 824, 826, 828, 830, 832 |
| `factory_catch_ball_turn_left_up` | 5 | 4.76 ± 0.43 | 844, 846, 848, 850, 852 |
| `factory_catch_ball_turn_right` | 5 | 6.92 ± 0.67 | 834, 836, 838, 840, 842 |
| `factory_catch_ball_turn_right_up` | 5 | 6.74 ± 0.58 | 854, 856, 858, 860, 862 |
| `factory_grab_stand_left` | 5 | 3.22 ± 0.37 | 684, 686, 688, 690, 692 |
| `factory_grab_stand_right` | 5 | 4.00 ± 0.47 | 694, 696, 698, 700, 702 |
| `factory_huibi` | 5 | 5.06 ± 0.05 | 704, 706, 708, 710, 712 |
| `factory_left_uppercut` | 5 | 12.62 ± 0.62 | 764, 766, 768, 770, 772 |
| `factory_lift_up` | 5 | 2.60 ± 0.14 | 674, 676, 678, 680, 682 |
| `factory_put_down2` | 5 | 21.18 ± 0.60 | 774, 776, 778, 780, 782 |
| `factory_right_uppercut` | 5 | 9.84 ± 0.27 | 754, 756, 758, 760, 762 |
| `factory_stepping` | 5 | 5.08 ± 0.63 | 794, 796, 798, 800, 802 |
| `factory_toulan_0` | 5 | 7.44 ± 0.55 | 734, 736, 738, 740, 742 |
| `factory_toulan_a` | 5 | 5.84 ± 0.61 | 724, 726, 728, 730, 732 |
| `factory_toulan_b` | 5 | 7.24 ± 0.27 | 714, 716, 718, 720, 722 |
| `factory_turn_left_eb1786c2` | 5 | 3.84 ± 0.36 | 864, 866, 868, 870, 872 |
| `factory_turn_left_fast` | 5 | 12.32 ± 0.61 | 874, 876, 878, 880, 882 |
| `factory_turn_left_small_step_a` | 5 | 4.90 ± 0.35 | 804, 806, 808, 810, 812 |
| `factory_turn_right_small_step_a` | 5 | 4.52 ± 0.43 | 814, 816, 818, 820, 822 |
| `factory_twist` | 5 | 13.90 ± 0.22 | 744, 746, 748, 750, 752 |

These measurements establish guarded balance completion for one execution. The phone was disconnected; distance, object handling, foot clearance and task efficacy remain unmeasured. Names and model displacement do not establish physical travel direction. Full score, gyro yaw, elapsed-time statistics, exact hashes, reports and source aliases are in the machine summary. No incumbent was replaced or claimed improved beyond noise.

## Model hold and remaining work

`factory_turn_left_0ea6ac20` was not executed physically. Its original passing report was `1ad75c60466f46f3ac7e4140a1a96d98`; a fresh simulation from the measured normal standing pose produced report `2c35432e1cbb4139b8f57a5d485469bd`, with model fall, peak tilt 93.52 degrees and end tilt 89.95 degrees. The fresh starting joints were within six raw counts of an approved stand endpoint. The old passing starting pose differed materially, and no approved complete endpoint matched it within the bridge's tolerance. Reusing the old report or inventing a special starting pose would not satisfy ordinary standing playback. This exact variant remains held.

The remaining 42 candidates require fresh state, calibration, starting-pose checks and guarded evidence. The next queued checkpoint, `factory_go_forward_end_adc8c18c`, was not released. The controller exited intentionally while idle after the last completed settle, job 883; no physical job was interrupted and no stop latch was created or cleared. Recovery actions retain their existing model holds; no substitute recovery loop was introduced.

## Artifacts and verification

Private artifacts are under `~/.local/share/tonypi/simulation/audits/1103-t18/`:

- `events.jsonl`: simulations, trial results, state checks, promotions and checkpoint decisions.
- `summary.json`: all 34 promotions, exact source mappings, matching jobs and ledger entries, metrics, remaining variants and model hold.
- `controller/`: preserved task scripts, candidate plans and logs for resuming and reproducing the read-only audit.
- `monitor.jsonl`: later periodic everyday status samples.
- `final-status.json` and `final-feet.jpg`: final state and post-movement floor observation.

The read-only summary generator revalidated all 34 saved promotions against model requests, current adapter identity, exact frames and calibration, distinct matching ledger/job digests, count one, 30-degree trial guards, completed jobs and no falls. It also matched all 101 new physical jobs to ledger evidence with no incomplete or unused trials. An independent read-only review checked the first 70 new trials and the 27 promotions then present, and separately investigated the held turn's starting-pose mismatch.

Existing factory import/audit, MCP profile, motion archive/catalog/promotion/runtime and simulation regression tests passed: **135 passed in 4.40 seconds**. No safety code, calibration, manufacturer source payload in git or verification gate changed.

Final everyday status confirmed idle, upright at 1.7 degrees, no latch, torque off, battery 11.904 V and hottest servo 45 C. The last settle completed; learning clients and the task monitor exited. The final camera observation was taken after all body and head movement, at epoch 1793721594.79168, head revision 9, pan 0 and tilt -40 degrees; it showed continuous wood floor without an edge. The everyday endpoint stayed in its everyday profile throughout, and final status reconfirmed it. No voice restart is needed.
