# Factory motion screening 1103-t25

2026-11-03. Eleven provisional approvals from twelve serial guarded physical screens; one physical failure and one remaining physical test blocked by unavailable recovery. Thirteen fresh model runs passed without clipping. This is single-trial completion evidence, not repeated-trial reliability or measured locomotion/object handling.

## Scope and method

Continued owner-authorized factory testing after t22/t23 and provisional integration commit `4f4d115`. The original thirteen-name shortlist was not persisted; the thirteen below were explicitly reconstructed from the prior forty-three remaining exact variants and saved before testing. Thirty other variants remain outside this shortlist. Prior five-trial approvals were left unchanged.

Carrying versus raised carrying versus neutral arm poses, backward cycle versus bounded step, and left/right balance sequences supply distinct roles. The slow forward variant is a timing alternative with no improvement claim. Factory names describe source intent; no ball or displacement effect was measured.

Each candidate used current sixteen-joint raw positions, unchanged tonyd trim/limits, source/frame digest checks, count one, upright home model base, stability objective, and a fresh unclipped simulation. Separate learning-profile MCP performed serial simulation_execute calls with guard_tilt_deg=30, followed by its existing settle behavior. No joint authoring, calibration changes, safety code changes, or overlapping body jobs. Preflight gates required idle, unlatched, untethered, upright <=10 degrees, battery >=10.9 V, and hottest servo <70 C. Actual pre-screen voltages for twelve attempted motions were 12.343–12.468 V, hottest 38–57 C. Phone was disconnected.

## Exact screens

Each successful row has one physical trial. Peak/yaw are gyro job measurements in degrees; each is a single observation. Settle jobs follow each screening job. Full variant hashes, exact frames, calibration, report/request digests, conditions and job/ledger evidence are in the private artifacts.

| Provisional name / candidate | Variant prefix | Model peak | Physical job | Physical peak | Yaw | Result |
|---|---|---:|---:|---:|---:|---|
| factory_catch_ball_go | 9dd9af0c | 5.9 | 939 | 11.7 | 5.9 | provisional |
| factory_catch_ball_go_slow | 015fc933 | 8.43 | 941 | 11.5 | -8.2 | provisional |
| factory_catch_ball_go_up | 4062c48e | 6.51 | 943 | 10.2 | 5.4 | provisional |
| factory_catch_ball_left_move | e7183a4e | 3.78 | 945 | 3.2 | -0.1 | provisional |
| factory_catch_ball_left_move_up | 0392f8a6 | 3.32 | 947 | 5.5 | -0.7 | provisional |
| factory_catch_ball_right_move | 526d6888 | 9.69 | 949 | 12.3 | 1.3 | provisional |
| factory_catch_ball_right_move_up | af006c87 | 7.82 | 951 | 10.2 | 1.7 | provisional |
| factory_back | c8226b0b | 5.81 | 953 | 5.0 | -1.5 | provisional |
| factory_back_one_step | c5b3768b | 7.7 | 955 | 4.3 | 6.4 | provisional |
| factory_left_move | 3ceb7394 | 4.47 | 957 | 3.2 | -0.4 | provisional |
| factory_right_move | 699cd04f | 10.23 | 959 | 6.6 | 1.3 | provisional |
| factory_tonypi_stand_left | 597b8438 | 11.67 | 961 | 43.3 | -5.9 | physical_hold |
| factory_tonypi_stand_right | ad0b0728 | 13.63 | — | — | — | blocked |

## Failure and recovery blocker

Left balance job 961 hit the 30-degree guard, reported peak filtered tilt 43.3 degrees and verdict failed after 13.26 s. Its subsequent settle job 962 reported raw peak tilt 153.3 degrees, end tilt 98.9 degrees and lying_after=true. The trial summary counted falls=0 because the later fall was outside that job’s metrics; post-job state at 96.5 degrees and the final camera image confirm a subsequent fall. Therefore the session includes one observed fall, and this candidate was not approved.

Right balance received only a model run (peak 13.63 degrees, end 1.17 degrees), using the newly read joint positions with the simulator’s upright home base. The real robot was sideways, so this model pass is not an eligible physical starting pose. Body work stopped. Daemon get_up job 963 refused with “ambiguous/sideways orientation; no verified get-up fits”. No low-level recovery or repeated get-up was attempted. A stable standing finish and the thirteenth physical screen remain blocked until the robot is upright. Final observation: lying, 88.4 degrees, torque off, idle, unlatched, 12.587 V, hottest 42 C. Final photo was captured after job 963.

## Promotion and everyday runtime verification

All eleven passing exact candidates were added through promote_provisional_skill to the private provisional registry. They have approval_level=provisional, model_verified=true, hardware_verified=false, reliability_verified=false, count_limit=1 and locomotion_unknown=true. Evidence gates revalidate exact frame content, calibration, report and matching no-fall physical jobs.

After 84 tests passed, tonypi-mcp was restarted to load 4f4d115. Direct HTTP MCP discovery on the everyday service confirmed exactly ten tools. Everyday status advertised all eleven additions with the correct provisional flags. A synchronous run_skill(factory_catch_ball_go,count=1,wait=true) correctly refused the current sideways body: “hardware trial requires an idle upright body with tilt <=10 deg”. Last job remained 963, proving this refusal created no body job. Successful everyday actuation remains untested because recovery is unavailable.

Validation: `uv run pytest -q tests/test_motion_provisional.py tests/test_mcp_profiles.py tests/test_simulation.py` — **84 passed in 4.04 s**. No voice restart or application code change was needed.

## Evidence locations

Private audit root: `/home/pi/.local/share/tonypi/simulation/audits/1103-t25/`.

- `shortlist.json`, `results.json`, `events.jsonl`: reconstructed shortlist, all outcomes and full serial event evidence.
- `controller/screen.py`, `learning-schemas.json`: published-tool learning controller and schemas.
- `runtime_check.json`, `runtime_status.json`, `recovery.json`: everyday discovery/availability/refusal and daemon recovery result.
- `final-photo.jpg`, `final-photo-metadata.json`: final sideways view captured after the last body job.
- `tests.txt`: relevant green tests.
- Simulation reports below live under `/home/pi/.local/share/tonypi/simulation/reports/<id>/`.

| Candidate | Fresh report ID |
|---|---|
| factory_catch_ball_go | `746eccbdc542422693d68d61ee6f31cd` |
| factory_catch_ball_go_slow | `db302e68a59841c5b532cf591324eff0` |
| factory_catch_ball_go_up | `ce1a7b8db4284612920dbec2ef900ff9` |
| factory_catch_ball_left_move | `b1d265dd2b5d45b5920f4d00e151e95f` |
| factory_catch_ball_left_move_up | `a59b0a46ffdb4b4caf213fd3cca8dd6f` |
| factory_catch_ball_right_move | `6d4dbcf411e04fefb27737cd7f3273fb` |
| factory_catch_ball_right_move_up | `7bdd739ba09046e890174c6e6d718e0f` |
| factory_back | `af30412019e24828839df912c7dc2eab` |
| factory_back_one_step | `e128925a36834069bd1536f45cca373d` |
| factory_left_move | `27c03f93a56e49c5a25e66e80733c219` |
| factory_right_move | `a9b72f8724f64d7ba36bf7b620e8c7ec` |
| factory_tonypi_stand_left | `450be4adc830402780bc5396314aabb9` |
| factory_tonypi_stand_right | `7d74569a3cbb4951bbd2311cc6fd9dbb` |
