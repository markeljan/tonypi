---
name: phone-observer
description: 'Use the iPhone as an outside observer (companion mode) to measure my own movement - placement, tracking, recording, reviewing episodes - or as mounted senses. Use before walk or displacement trials, to watch a new move, or when the owner sets the phone up.'
---

# Phone as observer

The phone never moves me. Companion = on a stand watching me (my main way to measure movement). Mounted = on my body
(its camera, LiDAR and ARKit pose are mine).

## Placement (speak this once, then verify yourself; ask nothing else)
When the phone isn't placed and a trial needs it, say with `tony say` (or [FINAL] in a conversation):
"Please put my phone on a stand, plugged in, about one and a half metres away at my hip height. My whole body and about
a metre of floor in front of me should be in view. Side-on for walking and leg lifts, facing me for turns and balance.
Say 'phone ready' when it's still. I'll check the view myself."
If nobody is around, do trials that need no phone (turns, balance, sweeps) and put the request in `tony inbox add`.

## Set up and check
1. `phone_status` (connected, battery, thermal). `phone_mode("companion")`.
2. `phone_snapshot`: am I fully in view, with floor in front? Too close (I fill the frame) or portrait at < 1 m
   leaves no room to walk into: ask for the placement above.
3. `phone_track("the small humanoid robot")`, then check the box covers me and not a chair (re-run with `box=[x,y,w,h]`
   from `phone_detect`/`phone_locate` if wrong). Tracking takes a few seconds to start.
4. `trial`/`compare` record automatically in companion mode (label `exp-<id>`). Manual: `phone_record("start",
   label, max_s)` -> move -> `phone_record("stop")`, or `phone_record_jobs(true)` to record every job.

## Read results
- `phone_episodes` / `phone_episode_review(id, frames=8)`: contact sheet plus summary. `target_moved_m` and the path
  are reliable for >= 10 cm (noise 1-2 cm). An episode with 0 tracked frames has no distance: fix tracking.
- Reliable: distance walked, falls (obvious in the sheet), the shape of a move. Not reliable: my heading (use gyro
  `yaw_deg`), joint angles, foot clearance under ~1 cm (judged only; store as judged), displacement while arms swing.
- `phone_pose` and `phone_motion` are the phone's own in companion mode, not mine.

## Rules
- No `phone_say` in companion mode (disabled: its speech comes back as a command). Speak with `tony say`.
- Requests heard by the phone mic come labelled; only Tony's own mic clears a stop latch.
- Mounted mode extras: `phone_ground_check` before walking (edges, drops), `phone_obstacles`, `phone_find` /
  `phone_locate` for bearings to targets, `phone_pose` for distance walked.
