---
name: when-stuck
description: 'Use when the same thing failed twice - a walk that doesn''t advance, a turn that doesn''t turn, a move that tips me over, a target that stays lost, a test that keeps failing. Stop repeating, gather data, try something genuinely different, keep what works.'
---

# When stuck

Rule: never try the same thing a third time.

1. Gather data:
   - feet: `look({pan_deg:0,tilt_deg:-40})` + `perceive({})`;
   - numbers: `status({})`, returned job yaw/tilt/outcome, phone tracking and `tony recall`;
   - recent shape: `perceive({mode:"context"})` or a phone episode;
   - code: full test output, `git diff`, `journalctl -u <service> -n 50`.
   Fine joint/video/ledger inspection belongs only in an owner-requested learning profile.
2. Name the cause in one sentence (`tony journal --kind note`). In a conversation, say it: "My feet catch on the rug
   edge, so I'm not moving forward."
3. Try a different available library action or route: side step, back up, another approach, or different head
   view/phone search. If the missing capability requires authoring/tuning, explain that and wait for the owner
   to request learning/practice/calibration. Never improvise joint frames, auto-enable learning, or bypass a refusal.
   In an authorized learning session, follow `body-learning` and simulate/verify every changed candidate.
   For code, read the failing path, write a failing test and ask a `researcher` subagent if needed.
4. After 2-3 different ideas without progress: in a conversation, ask the owner one specific question; in an
   autonomy session, `tony goal block <id> "<what blocks it>"` and move on to the next goal.
5. Keep what worked: record the library action and measurements; learning candidates need the promotion gate.
   Update the matching skill text, `tony remember --kind lesson`.
