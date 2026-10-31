---
name: find-object
description: 'Find an object that isn''t in view ("where are my keys", "find the ball"). Memory first, then systematic camera and phone search with yaw bookkeeping, confirm, report, remember.'
---

# Find an object

1. Memory: `tony recall "<object>"` and MEMORY.md ## Home. A remembered place is a hint; still verify with a camera.
2. Phone first if connected: `phone_find("<class>")` (80 COCO classes: cup, bottle, chair, sports ball...) or
   `phone_locate("<description>")` (anything in words, takes seconds). Both give bearing and distance.
3. Head camera without walking: `look({pan_deg,tilt_deg})` then `perceive({})` across left/ahead/right.
   Small floor objects: tilt -30; level next; tilt 15 for shelves. Keep a record of views already checked.
4. Not found: use short `move({direction:"turn_left",steps:1})` jobs with returned gyro yaw to cover a full circle
   once, observing after each leg. Never assume a fixed angle per step.
5. Still not found: if an available gait and budget permit, move to one new vantage point and repeat.
6. Seen it: confirm colour/shape with a centred `look` and `perceive`; a remembered location is only a hint.
7. Answer where it is relative to me and the room: "Your keys are on the table by the door, to my right."
8. `tony remember --kind home "<object>: <place>"`. If asked to go there, continue with `walk-to-target`.
Out of options after a full circle from two places: say where I looked.
