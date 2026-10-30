---
name: walk-to-target
description: Walk to something ("walk to the chair", "go to the door"), even when it starts off to the side or out of view. Scan, turn toward it, walk in short checked legs, arrive, confirm.
version: 2.0.0
metadata:
  hermes:
    tags: [robot, camera, navigation]
    category: robot
---

# Walk to a target

Start: `tether()` (walking steps left; turns are free), `get_state` (upright? heading_deg).

1. Find it: `scan()` (left 60° / ahead / right 60°, labelled with the gyro heading). Not seen: `turn_left(2)` (or
   right) with guard_tilt_deg=35 and `scan()` again; a full look-around is ~5-6 turns of 2. Still not: see
   find-object. Note where it is (pan angle in the scan, or its heading) and roughly how far (size in the picture).
2. Face it: head centred, turn the body toward it. Use the job's `yaw_deg` to know how far you actually turned
   (roughly 10-25° per turn step, varies with the floor), then `snapshot` to check it is ahead and near the centre.
3. Walk in legs: `walk_forward` 2-3 steps with guard_tilt_deg=35, then `snapshot`. Re-centre with a 1-step turn when
   it drifts off centre. Budget the tether steps: if it's far, spend them on the last metre, not the first.
4. Arrived when it fills a big part of the picture or is at your feet (`look(0, -30)`) and the next step would bump
   it. Stop short; don't push into furniture.
5. Obstacles: step_left/step_right around them, or turn and go around. A fall: wait for the keeper to get you up,
   `snapshot`, carry on. A refused walk (tether): get as close as you can with what's left, then say so.
6. `task_done(success=true, summary="I'm at the chair.", how="scanned, turned ~40° left, 3 legs of 2 steps")`.
   If this taught you something about the room (where the chair is from the start spot), save it to memory.
