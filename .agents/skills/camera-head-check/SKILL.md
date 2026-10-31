---
name: camera-head-check
description: Diagnose or verify camera/head concurrency after an owner-approved camera or daemon change; never enables learning or clears a stop latch.
---

# Camera/head maintenance check

Read `docs/CAMERA_HEAD.md` and `src/tonypi/tonyd/AGENTS.md`. Use fake-hardware tests first:
`uv run pytest -q tests/test_camera_head.py tests/test_tonyd.py tests/test_video_context.py tests/test_mcp_profiles.py`.
On macOS, pass a short `--basetemp=/tmp/tonypi-camera-tests` if Unix socket paths exceed the platform limit.

Camera capture, JPEG sampling and head commands have separate lifetimes. Reproduce contention with a blocked
camera request and a head/stop on the **same TonydClient**. Separate-client success does not prove this is fixed.
Check capture-time pose metadata by changing the head during read/encoding; never infer capture pose from current state.
The regression suite covers delayed scan cancellation, one-axis legacy gestures, stop latch restart and bounded files.

For deployment, inspect remote git state and fresh state, then restart only the services the owner authorized, only
while idle. A pre-upgrade daemon does not persist its latch: when upgrading from it, preserve an already-set latch in
`/run/tonypi/stop-latched` before restarting. The new daemon reads it before accepting commands. Never clear it as a test.
Do not start a muted voice service.

Use `perceive` and context for read-only live checks. Small head moves require explicit owner authorization, an
unlatched idle robot, fresh upright/resting IMU state, adequate battery and cool servos. Use published `look`/head
commands only, save and restore a known initial pose, and verify actual PWM readback if the maintenance interface
provides it. Never walk or move the body for this check. If any condition fails, leave a checklist and record why.
