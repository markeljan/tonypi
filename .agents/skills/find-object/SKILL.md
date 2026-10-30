---
name: find-object
description: 'Find an object that isn''t in view ("where are my keys", "find the ball"). Memory first, then systematic camera and phone search with yaw bookkeeping, confirm, report, remember.'
---

# Find an object

1. Memory: `tony recall "<object>"` and MEMORY.md ## Home. A remembered place is a hint; still verify with a camera.
2. Phone first if connected: `phone_find("<class>")` (80 COCO classes: cup, bottle, chair, sports ball...) or
   `phone_locate("<description>")` (anything in words, takes seconds). Both give bearing and distance.
3. Head camera without walking:
   - small things (shoes, keys, a cup on the floor): `scan(tilt_deg=-30)` first;
   - then `scan()` level, and `scan(tilt_deg=15)` for tables and shelves.
4. Not found: turn ~90 deg (`turn_left`, guard 40), keep a running total of `yaw_deg`, scan again. One full circle
   (~360 deg) once, not the same side twice.
5. Still not found: move a few steps to a new vantage point (within the tether budget) and repeat.
6. Seen it: confirm with a centred, closer `snapshot` (`look(pan, tilt)` or a 1-step turn). Check colour and shape.
7. Answer where it is relative to me and the room: "Your keys are on the table by the door, to my right."
8. `tony remember --kind home "<object>: <place>"`. If asked to go there, continue with `walk-to-target`.
Out of options after a full circle from two places: say where I looked.
