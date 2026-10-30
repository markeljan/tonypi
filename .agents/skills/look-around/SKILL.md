---
name: look-around
description: 'Look around the room with the head camera (and the phone when mounted) and say what is where. Use for "what do you see", "look around", "where am I", and when mapping the home.'
---

# Look around

1. `scan()`: photos at 60 deg left, ahead and 60 deg right, labelled with the gyro heading; the head returns.
   Tables and shelves: `scan(tilt_deg=15)`. Floor: `scan(tilt_deg=-30)`.
2. Answer in one or two spoken sentences: "On my left a sofa, ahead the door, on my right a table with a lamp."
3. Images stay in my context and slow every later turn: one scan answers the question; don't repeat it unless I moved.
4. Lasting facts about the room: `tony remember --kind home "<room>: <what is where>"` (goal g5 grows from these).
