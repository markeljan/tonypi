---
name: walk-to-target
description: 'Walk to something ("go to the chair", "walk to the door"), even when it starts off to the side or out of view. Find it, turn toward it by gyro, walk in short checked legs, arrive, confirm, remember where it is.'
---

# Walk to a target

1. `tether()` (walking steps left; turns are free) and `get_state`. `tony recall "<target>"` for where it was.
2. Find it: `snapshot`; not in view -> `scan()`; still not -> turn 90 deg and `scan()` again (see `find-object`).
   Mounted phone: `phone_find` / `phone_locate` give bearing and distance directly.
3. Bearing from the head camera: pan - (x_px - 320) x 0.12 deg (640 px wide, ~74 deg view; + = left, so a target
   right of the image centre gives a negative bearing = turn right).
4. Face it: `run_skill("turn_left"|"turn_right", count, guard_tilt_deg=40)`; sum the jobs' `yaw_deg` until it matches
   the bearing (~20 deg per turn_left step, turn_right varies; measure, don't assume). `snapshot`: target near centre.
5. Walk legs: `run_skill("walk_forward", 2..3, guard_tilt_deg=40)`, then `snapshot`, re-centre with a 1-step turn.
   Compare fixed things in the photos to confirm I moved; a "done" walk on a rug can go nowhere.
6. Arrived when the target is big and low in the frame or at my feet (`look(0, -30)`). Stop short; don't push into it.
7. No progress after 2 legs: look at my feet (`look(0, -40)` + `snapshot`) and follow `when-stuck` (rug edge -> high
   step, another angle, side step, back up one step).
8. Tether budget out: get as close as possible, then say how far I got.
9. Report in one sentence; `tony remember --kind home "<target>: <where, relative to what>"`.
A fall on the way: `get_up`, `snapshot`, carry on.
