---
name: find-and-approach
description: Find an object or place with the camera and walk toward it (e.g. "find the door and walk to it").
version: 1.0.0
metadata:
  hermes:
    tags: [robot, camera, navigation]
    category: robot
---

# Find and approach

1. `scan` (left / ahead / right photos in one call). Not found: `run_skill("turn_left", 2)` and `scan` again (at most
   4 times), then give up and say so. Never more than 2 turn steps per run_skill call (longer turns topple Tony).
2. Target seen at a head pan angle P: centre the head (`look(0, 0)`), then turn the body toward it:
   P > 20 → `turn_left` (1 step per ~20°), P < -20 → `turn_right`.
3. `run_skill("walk_forward", 2)`, then `snapshot`. Re-centre with small turns if it drifted off centre.
4. Repeat 3 until the target fills most of the picture or is at your feet (look down: `look(0, -30)`), then stop and say
   you're there. At most ~10 rounds.
5. Anything in the way (wall, furniture, an edge or drop): stop and say so. Never walk off a table edge.
6. Be quick: one snapshot per decision is enough (the floor ahead is in it). Don't look down or re-check before every
   step, and move within the first few tool calls: a minute of looking without moving is a failure.
