---
name: find-object
description: 'Use when finding an object not in view. Scan, turn, move, verify, and report its location; check the floor first for small objects.'
version: 1.0.0
metadata:
  hermes:
    tags: [robot, camera, search]
    category: robot
---

# Find an object

1. Memory first: you may know where it usually is (memory / earlier sessions). Still verify with the camera.
2. Look around without walking. For small objects, such as shoes or a cup, check the floor FIRST with
   `scan(tilt_deg=-30)` (60° left / ahead / 60° right) before any body turn; small objects nearby can be below
   the forward camera view. Inspect those images before deciding to turn. If not found, use `scan()` for
   the level view and `scan(tilt_deg=15)` for tables. Then `turn_left(2)` with guard_tilt_deg=35 and scan again;
   for small objects, start each new heading or vantage point with the downward scan. Keep track of the total
   `yaw_deg` so you cover the full circle (~360°) once, not the same side twice.
3. Not in the room view from here: move to a new vantage point (a few steps toward the open part of the room,
   within the tether budget) and repeat. Look under/behind things by changing the head tilt.
4. Seen it: confirm with a closer `snapshot` (centre it with `look(pan, tilt)` or a 1-step turn); be sure it's the
   right thing (colour, shape). If asked to go there, continue with walk-to-target.
5. Answer where it is relative to Tony and the room ("Your shoes are by the door, to my right."), then
   `task_done(success=true, summary, how)` and save the location to memory.
6. Out of options after a full circle from two places: `task_done(success=false)` saying where you looked.
