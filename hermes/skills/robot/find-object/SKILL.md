---
name: find-object
description: Find something that is not in view ("find my shoes", "where's the red cup?"): systematic scan, turn, move, verify, report where it is.
version: 1.0.0
metadata:
  hermes:
    tags: [robot, camera, search]
    category: robot
---

# Find an object

1. Memory first: you may know where it usually is (memory / earlier sessions). Still verify with the camera.
2. Look around without walking: `scan()` (60° left / ahead / 60° right), also `scan(tilt_deg=-30)` for things on the
   floor (shoes!) and `scan(tilt_deg=15)` for things on tables. Then `turn_left(2)` with guard_tilt_deg=35 and
   scan again; keep track of the total `yaw_deg` so you cover the full circle (~360°) once, not the same side twice.
3. Not in the room view from here: move to a new vantage point (a few steps toward the open part of the room,
   within the tether budget) and repeat. Look under/behind things by changing the head tilt.
4. Seen it: confirm with a closer `snapshot` (centre it with `look(pan, tilt)` or a 1-step turn); be sure it's the
   right thing (colour, shape). If asked to go there, continue with walk-to-target.
5. Answer where it is relative to Tony and the room ("Your shoes are by the door, to my right."), then
   `task_done(success=true, summary, how)` and save the location to memory.
6. Out of options after a full circle from two places: `task_done(success=false)` saying where you looked.
