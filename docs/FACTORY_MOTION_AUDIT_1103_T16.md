# Factory motion audit 1103-t16

Continuation of [1103-t15](FACTORY_MOTION_AUDIT_1103_T15.md). The source simulation audit is complete. Physical testing remains blocked by unresolved floor support; no motion was physically validated or promoted. No safety code, calibration, motion payloads or approval gates were changed.

The subsequent owner-confirmed floor trials and hardware promotions are recorded in
[1103-t17](FACTORY_MOTION_AUDIT_1103_T17.md). The readiness statements below describe the earlier t16 session.

## Completed source audit

The existing audit tool inventoried 317 assets and evaluated all **160 distinct body sequences** with the current software calibration and a read-only EEPROM-limit snapshot. Channels 17/18 were explicitly omitted. All assets passed the hardware frame-format and EEPROM-limit checks; there were no import or simulation errors.

| Source | Assets | Distinct sequences within source | Unclipped model passes |
| --- | ---: | ---: | ---: |
| Installed imports | 83 | 82 | 38 |
| Pinned PAB47 tree | 118 | 118 | 57 |
| Official manufacturer tree | 116 | 116 | 60 |
| Combined, deduplicated | 317 | 160 | 77 |

The per-source pass counts overlap and must not be added. Installed `go_forward_end` and `go_forward_power_end` have identical body content, so the 38 passing installed sequences represent 39 assets, consistent with t15's 39 passing recordings.

| Exact-sequence outcome | Count |
| --- | ---: |
| Physics passed, no model or software clipping | 77 |
| Physics passed, model joints clipped | 29 |
| Physics failed, model joints clipped | 46 |
| Physics failed, no clipping | 8 |
| Total | 160 |

Thus 106 physics runs returned success, but only **77** passed the complete model gate. The other **83** remain unverified. No software-limit clipping occurred. The run reused 16 complete, exact-request cached reports and produced 144 new source reports. Seven additional measured-pose simulations below are separate from these 160 cases.

The eight unclipped failures were `chest`, `left_kick`, `right_kick`, `turn_right_fast`, PAB `fall_back`, PAB `turn_right`, official `temp1`, and official `turn_right`. For example, `chest` peaked at 46.98 degrees, above the model's 45-degree stability threshold, despite ending upright. The two `turn_right` source variants remain distinct.

Provenance is unchanged: [official revision dc452eaa](https://github.com/Hiwonder/TonyPi/tree/dc452eaac9991516600860f706f4b889f3305d1d/ActionGroups) and [PAB47 revision 97807f5e](https://github.com/pab47/TonyPi/tree/97807f5e26049ce1c3d90b1c80d5d2a92606d40f). Reports use MuJoCo 3.17.0 and record the adapter, request digest, current calibration and exact contents. These are model results, not measured travel, gesture quality or reliability.

## Physical readiness and concrete blocker

Owner authorization for learning and physical tests was present. Preflight measured 11.048 V, hottest servo 35 C, upright, idle, no stop latch and no tether. These passed the voltage and temperature thresholds. The phone was disconnected; that alone does **not** prevent the static standing/arm trials below, whose balance results can be measured by the IMU and authoritative jobs.

Head-camera checks used pan 0/-60/+60 at tilt -40 and pan 0 at tilt +25. Settled views showed a nearby wooden surface, a perforated white panel, the torso, and the room/desk/person. They did not reveal contact under either foot or establish the support height. The side views were substantially obscured by the torso. A wooden surface and an upright IMU reading do not establish that both feet are supported on the floor rather than a raised surface. **Actual floor support remains unresolved through the available views**, so no body trial was started. This is the physical blocker; it is not a battery, temperature, phone-measurement or simulation failure.

The final settled camera frame was captured after the last head move, at pan 0, tilt -40 (capture epoch 1793719064.5803883, head revision 7). Final everyday status confirmed head settled, tilt 2.4 degrees, lying false, idle, torque off, 11.141 V, hottest servo 36 C, no latch and no active job. The body pose was unchanged throughout.

Both everyday get-up actions are also unavailable under the existing model gate. All three distinct source recovery sequences failed their recovery simulations and clipped model joints: back ended at 89.94 degrees, official/installed front at 98.06 degrees, and PAB front at 98.06 degrees. Recovery availability is a separate limitation to report, not an invented preflight prohibition on otherwise eligible standing tests. No recovery or safety bypass was attempted.

## Next physical candidates

Seven existing installed standing/arm sequences were additionally simulated from all 16 freshly measured raw joint positions, with `start=home`, `count=1`, `objective=stability`. Each passed without clipping and was marked a hardware candidate. The model translates the feet onto an ideal floor; this does not resolve the real support blocker.

| Motion | Simulated peak / end tilt, degrees | Measured-pose report ID |
| --- | --- | --- |
| `both_arms_up` | 6.62 / 2.08 | `194432b1e925460e9e20cc9eda7441fb` |
| `stand` | 2.53 / 2.16 | `103595c4898d4d87ac2a98fab5745d16` |
| `stand_slow` | 2.47 / 2.17 | `768db5eaa13049d7865ae19e6c427a50` |
| `wave` | 2.37 / 1.43 | `150e4e9aee864467aa857bf06f1ec360` |
| `left_hand` | 2.53 / 2.04 | `f4bb38035cd944e68e37d2c81e909b20` |
| `lift_left_hand` | 3.06 / 1.61 | `f9db9401f9824ee7aa2b7fb61e06fec5` |
| `right_hand` | 2.53 / 2.04 | `6499f32d6a3e4abfbff7e7dff6ac2d02` |

Once sensors establish actual floor support, start with `stand` and `wave`. Refresh state and all raw joints before execution; these reports are usable only while their content/calibration is unchanged and joints remain within the bridge's 20-count tolerance. Otherwise re-simulate from the fresh measured pose. Use `simulation_execute` for one existing 30-degree-guarded Lab trial at a time, inspect the result, and stop on cancellation, latch or unavailable recovery. Five distinct successful matching jobs with zero falls are required before `promote_skill`; separate starting-pose reports can contribute only when exact executed frames and calibration match. No phone distance claim is needed for these standing balance checks.

No physical trial means **0 new physical successes, 0 new promotions**. The ten existing model-approved everyday actions retain their previous availability and hardware-acceptance-pending status.

## Artifacts, verification and profile

Machine artifacts remain outside git, with no manufacturer frames added to the repository:

- `~/.local/share/tonypi/simulation/audits/1103-t16/all-sources.json`: all 317 source assets, exact-body hashes, aliases, limit checks and 160 report references.
- `~/.local/share/tonypi/simulation/audits/1103-t16/summary.json`: source totals and recovery results.
- `~/.local/share/tonypi/simulation/audits/1103-t16/current-pose-simulations.json`: measured initial raw positions and the seven complete pose-specific results.
- `~/.local/share/tonypi/simulation/reports/<report-id>/`: exact request, physics trace and report.

Reproduce the source audit with:

```sh
uv run python tools/audit_factory_motions.py \
  --official-repo /tmp/tonypi-1103-t15/Hiwonder-TonyPi \
  --output /home/pi/.local/share/tonypi/simulation/audits/1103-t16/all-sources.json
```

Validation: **135 passed in 5.10 s**, using `tests/test_factory_audit.py`, `test_factory_import.py`, `test_mcp_profiles.py`, `test_motion_archive.py`, `test_motion_catalog.py`, `test_motion_promotion.py`, `test_motion_runtime.py` and `test_simulation.py`. No implementation changes were needed. Learning calls used short-lived separate stdio processes; they have exited. Everyday `status` confirmed `profile=everyday`. No voice or MCP service restart is needed.
