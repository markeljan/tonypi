---
name: look-around
description: Scan the room with the head camera (left, centre, right) and report what is where.
version: 1.0.0
metadata:
  hermes:
    tags: [robot, camera]
    category: robot
---

# Look around

1. `look(pan_deg=50)`, `snapshot` → what's on the left.
2. `look(pan_deg=0)`, `snapshot` → what's ahead.
3. `look(pan_deg=-50)`, `snapshot` → what's on the right.
4. `look(0, 0)` to finish facing forward.
5. Answer in one or two spoken sentences: "On my left there's a sofa, ahead a door, on my right a table."
   If you learned something lasting about the room, save it to memory (e.g. "living room: door is opposite the sofa").
