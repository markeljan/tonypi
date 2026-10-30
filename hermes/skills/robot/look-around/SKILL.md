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

1. `scan()`: photos at 60° left, ahead and 60° right with the gyro heading (the head returns to centre).
2. Answer in one or two spoken sentences: "On my left there's a sofa, ahead a door, on my right a table."
   If you learned something lasting about the room, save it to memory (e.g. "living room: door is opposite the sofa").
