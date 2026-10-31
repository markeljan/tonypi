# Camera and head concurrency

`tonyd` remains the only camera/servo owner. Its capture thread continuously reads UVC frames at the configured
15 fps and publishes one frame plus its metadata under a short lock. No camera, body-job or command lock is held
across an exposure. JPEG encoding uses a reference to that published frame outside the camera lock.

The camera was already threaded before this fix. The reproducible contention came from sequential socket dispatch:
`scan`/`burst` occupied the connection shared by MCP head and stop commands. Snapshot freshness polling also waited
up to 1.5 seconds and could return an older frame after timing out. There was no camera/body-job-lock refusal in the
investigated code, and retained logs did not prove the precise cause of the owner's reported incident.

Camera requests now run outside command dispatch, with two process-wide slots and no waiting queue. Capacity
exhaustion returns an error; head and stop still run. Head is a short PWM command, never a body job. It serializes
only its send/metadata update against stop and other head commands. Stop latches also refuse head movement and
persist in `/run/tonypi/stop-latched` across service restarts; the existing authorized unlatch removes that marker.
Startup no longer centers the head, so an unknown startup head pose stays explicitly unknown until commanded.
Legacy head gestures use the same timeline and preserve single-channel writes.

Each snapshot carries `captured_monotonic_s`, `captured_epoch_s`, capture-read start time, `head_deg`,
`head_revision`, `head_pose_known`, `head_moving`, and timestamp/pose basis. UTC is UVC dequeue time, **not a sensor
exposure timestamp**. Angles are commanded targets, **not encoder measurements**. Frames whose read interval
crosses a command, head travel/settle interval, or conservative two-frame buffering margin are labelled moving.
A raw snapshot exposes that label; rolling history skips such frames and retains daemon timestamps and pose tags.
The history remains capped at 2 fps, 120 frames, 60 seconds and 8 MiB; completed snapshot files are capped at 40,
including when encoders complete out of order.

A scan moves the head, waits a fixed settling interval including UVC buffering margin, and samples the latest
published frame. Freshness is a filter: it never waits on the camera for a future exposure and never substitutes a
stale/moving frame. A stalled camera produces a clear error. A newer head command or stop cancels the scan; a
cancelled scan never returns the head to its old target. A dispatch-time generation/revision token also prevents a
scan worker that starts late from moving after a stop or newer head request.

## Verification

The fake-hardware suite `tests/test_camera_head.py` covers same-client head/stop during blocked snapshot, scan
and burst, a stalled camera exposure, capture/encoding pose races, capacity limits, delayed scan cancellation,
legacy one-axis writes, stop latch restart and snapshot retention. `tests/test_video_context.py` checks history
bounds and capture metadata. Existing daemon, motion-runtime and MCP-profile tests protect the integrated path.

For a supervised live head check, only after the owner requests movement and the stop latch is clear:

1. Read fresh state. Require idle, fresh IMU, standing or safely resting, >=10.9 V and hottest servo <70 C.
2. Record a known initial head pose. Start a bounded burst and send a small head change (for example +5 degrees
   pan and -3 degrees tilt) through the same client while capture continues. No body motion.
3. Confirm prompt command completion, unchanged body-job history, advancing capture timestamps, moving labels
   during the turn and settled pose tags afterward. Check hardware PWM readback or visually verify actual motion;
   a successful command alone does not prove physical head movement.
4. Restore the initial head pose. Confirm the camera and MCP context remain healthy. Keep voice muted if requested.

Fast-loop tracking is deferred. It needs its own detector/control-loop validation and an explicit decision about
the ten-tool everyday interface; this camera fix does not add tracking tools.
