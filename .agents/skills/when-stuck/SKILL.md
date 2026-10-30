---
name: when-stuck
description: 'Use when the same thing failed twice - a walk that doesn''t advance, a turn that doesn''t turn, a move that tips me over, a target that stays lost, a test that keeps failing. Stop repeating, gather data, try something genuinely different, keep what works.'
---

# When stuck

Rule: never try the same thing a third time.

1. Gather data (one retry may be part of this):
   - feet: `look(0, -40)` + `snapshot` (rug edge, cable, chair leg, slippery spot?);
   - the move itself: `run_skill(..., wait=false)` then `video(3, 8)`, or a phone episode (`phone-observer`);
   - numbers: the jobs' `yaw_deg` (did I turn?), `max_tilt_deg` (did I wobble?), `get_joints` after the move (did the
     joints reach their targets?), `tether()` (budget left?), `ledger` for past tries;
   - code: the failing test's full output, `git diff`, `journalctl -u <service> -n 50`.
2. Name the cause in one sentence (`tony journal --kind note`). In a conversation, say it: "My feet catch on the rug
   edge, so I'm not moving forward."
3. Try something genuinely different:
   - obstacle under the feet: a high-step motion (`motion-authoring`), cross at an angle, side-step, back up and
     approach elsewhere;
   - a turn that doesn't turn: a tuned turn from `library()`, smaller steps, side steps plus a turn, another surface;
   - a move that tips me: slower frames, smaller leg changes, wider stance, arms out for balance;
   - a lost target: head tilted down or up, another spot, the phone (`phone_locate`);
   - code: read the code path again, write a failing test first, ask a `researcher` subagent.
4. After 2-3 different ideas without progress: in a conversation, ask the owner one specific question; in an
   autonomy session, `tony goal block <id> "<what blocks it>"` and move on to the next goal.
5. Keep what worked: save the motion or skill, update the matching skill text, `tony remember --kind lesson`.
