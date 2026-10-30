# tonyd: the body daemon (safety code)

tonyd is the only process on `/dev/ttyAMA0` (servo board, IMU, battery) and the camera. Every brain, the MCP server,
the learning runner and the phone bridge reach the body through its socket `/run/tonypi/tonyd.sock` (JSON lines,
`client.TonydClient`).

## Rules for changing this directory
- This is safety code. Changing or restarting it needs the owner's yes first. Then run
  `uv run pytest -q tests/test_tonyd.py` and restart only when they pass and Tony is idle in a stable pose (sitting or
  lying): `sudo systemctl restart tonypi-tonyd`. Never mid-motion.
- Keep the reflexes no brain can bypass: `stop` and its latch (motion refused until the owner speaks again), the
  temperature guard (skip a motion at >= 81 C, stop and unload at 83 C), torque off 1.5 s after the last job, the 20 s
  idle watchdog, the tether step budget.
- tonyd never stands Tony up, settles him or stops a move for tilting by itself. Falls are the brain's job; a job can
  opt in to `guard_tilt_deg`.
- Never open the board or camera from another process. `tools/bench.py` needs `sudo systemctl stop tonypi-tonyd` first.

## Files
- `server.py` socket server, jobs, job log. `body.py` motion execution, temperature and tilt guards.
- `skills.py` the only list of built-in skills. `studio.py` user motions, skills, calibration (user layer in
  `~/.local/share/tonypi/`). `camera.py` capture loop and snapshots. `client.py` the client every caller uses.

## Ops
`ping, skills, run {skill, count 1-5, wait, guard_tilt_deg, trace}, play {frames, hold_s, guard_tilt_deg}, pose,
torque, stop, unlatch, state, job {id}, joints, head {pan, tilt}, snapshot, scan, burst, relax, tether, motions,
motion_get/save/delete, skill_save/delete, calibration, servo_calibration, servo_calibrate {id, offset, limits, save,
reason}`, plus `subscribe` for events.
- Every finished job is appended to `~/.local/share/tonypi/jobs.jsonl` (wall-clock t_start/t_end, max_tilt_deg,
  yaw_deg, rot_deg, end tilt, lying_after, hottest servo before/after, battery). The last 200 stay in memory for `job`.
  `trace: true` writes an IMU trace to `~/.local/share/tonypi/traces/<id>.jsonl`.
- `servo_calibrate` writes servo EEPROM: owner's yes plus a reason; every call is logged to
  `servo-calibration-log.jsonl`.
