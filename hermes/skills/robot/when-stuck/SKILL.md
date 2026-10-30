---
name: when-stuck
description: Use when the same thing has failed twice (a walk that doesn't advance, a turn that doesn't turn, a move that tips Tony over, a target that stays lost). Stop repeating it, gather data, say what's wrong, try a genuinely different approach, ask the user if needed, keep what works.
version: 1.0.0
metadata:
  hermes:
    tags: [robot, recovery, learning]
    category: robot
---

# When stuck

Rule: never try the same thing a third time. After 2 failed tries:

1. Gather data, while trying once more if needed:
   - look at your feet: `look(0, -40)` + `snapshot` (a rug edge, a cable, a chair leg, a slippery spot?)
   - `play_frames(..., wait=False)` or `run_skill(..., wait=False)` then `video(3, 8)`: watch what the feet do
   - compare the jobs' `yaw_deg` (did you turn?), `max_tilt_deg` (did you wobble?), before/after snapshots of the same
     fixed things (did you move?), `get_joints` after the move (did the joints reach the targets?), `tether()`.
2. Say what's wrong in one short sentence (`say`): "My feet catch on the rug edge, so I'm not moving forward."
3. Try something genuinely different, outside the box if needed:
   - an obstacle under the feet (rug edge, threshold): write a high-step motion that lifts each foot higher (knee and
     hip pitch more, slower frames) with the new-motion skill; or cross it at an angle, side-step onto it, back up
     and approach elsewhere
   - a turn that doesn't turn: a tuned turn, smaller steps, side-steps plus a turn, or turn on a different surface
   - a move that tips Tony over: slower frames, smaller leg changes, wider stance, arms to balance
   - a lost target: scan with the head tilted down/up, from another spot, ask where it is
4. Ask the user when you are not sure what's happening or have tried 2-3 different ideas:
   `ask_user("I'm stuck at the rug edge, what should I do?")` or `ask_user("What's going on? My walk isn't moving
   me.")`. Use the answer. Asking is good; looping silently or giving up is not.
5. Keep what works: save the motion/skill as NAME_hermes, and write the lesson into the matching skill
   (skill_manage: walk-to-target, find-object, new-motion ...) so next time you start with what works.
