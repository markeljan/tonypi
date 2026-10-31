---
name: look-around
description: 'Look around the room with the head camera (and the phone when mounted) and say what is where. Use for "what do you see", "look around", "where am I", and when mapping the home.'
---

# Look around

1. Use `look({pan_deg,tilt_deg})` then `perceive({})` for left, ahead and right as needed.
   Tables/shelves: tilt 15; floor: tilt -30. Return the head to centre when done.
2. Answer in one or two spoken sentences: "On my left a sofa, ahead the door, on my right a table with a lamp."
3. Images stay in my context and slow every later turn: keep views few; don't repeat it unless I moved.
4. Lasting facts about the room: `tony remember --kind home "<room>: <what is where>"` (goal g5 grows from these).

## Recent visual history

Everyday: `perceive({mode:"context"})` returns a recent mosaic. Do not infer physical distance from it.
The detailed options below are for the explicit learning profile only.

For what changed or moved, call `video_context(frames=24, seconds=12, end_ago_s=0)`: one mosaic, oldest first,
left-to-right then top-to-bottom, with UTC labels and metadata (ages, head angles, capture errors). For one recent
view use `frames=1`; for a prior interval use e.g. `frames=8, seconds=10, end_ago_s=20` (30 to 20 seconds ago).
Frames are sampled continuously at 2 fps while tonypi-mcp runs, stored at 320x240 JPEG quality 70, and evicted after
60 seconds or the 120-frame / 8 MiB limits. Choose 1-24 output frames; seconds + end_ago_s must be <=60.
Startup, disconnection and slow sampling can leave gaps or fewer frames; check metadata before describing timing.
UTC capture times are estimates from tonyd's frame age, with encoding and socket latency. Camera/head motion also
changes the view: a mosaic alone does not prove object displacement. Use snapshot for current full-resolution detail.
The buffer runs locally without automatic model calls or archival video; it clears on MCP restart. The brain sees
history when it asks, so this does not by itself provide continuous attention or fast head/body tracking.
Sampling uses tonyd's existing snapshot API and its shared 40-file rotation: raw snapshot paths are temporary
(about 20 seconds at idle with this sampler), so read them promptly. The buffer copies its own JPEG bytes into
memory. Existing MCP snapshot/scan/video tools consume their files within their bounded calls.

Implementation evidence: `tests/test_video_context.py` checks time windows, eviction, byte caps, mosaic order,
camera recovery and MCP lifecycle with generated frames and no robot movement.
