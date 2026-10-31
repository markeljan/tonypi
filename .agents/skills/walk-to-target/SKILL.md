---
name: walk-to-target
description: 'Walk to something ("go to the chair", "walk to the door"), even when it starts off to the side or out of view. Find it, turn toward it by gyro, walk in short checked legs, arrive, confirm, remember where it is.'
---

# Walk to a target

1. `status({})`: available verified actions, body/job state, battery, temperature, latch and any tether budget.
   `tony recall "<target>"` for where it was. A latch/refusal stops body work.
2. Find it with `perceive({})`; use `look` then perceive for head views (see `find-object`). A mounted phone's
   `phone_find` / `phone_locate` gives bearing and distance. Never treat a companion phone's pose as Tony's pose.
3. Face it with short `move({direction:"turn_left"|"turn_right",steps:1})` jobs; inspect returned gyro yaw and
   re-observe. Do not assume a fixed angle per step or a distance from image size.
4. Walk short legs with `move({direction:"forward",steps:1})`, then perceive and re-centre as needed.
   Distance claims require validated phone tracking, not completed step counts or screenshots.
5. Stop short of the target. A large/low image is a proximity hint, not measured arrival. Check depth when available.
6. No progress after two legs: inspect feet with `look({pan_deg:0,tilt_deg:-40})` and perceive, then follow
   `when-stuck`. Choose an available side step/backward action or another route. Missing high-step capability
   is reported; it does not authorize motion authoring.
7. If budget is exhausted or recovery fails, stop and report how far the measurements establish. tonyd handles
   fall recovery/resume; do not build a joint-control recovery loop or bypass a stop latch.
8. Report one sentence; `tony remember --kind home "<target>: <where, relative to what>"`.
