---
name: tonypi-simulation
description: 'Discover PAB47/TonyPi actions and Tony motions, simulate a motion or proposed keyframes in MuJoCo, inspect model-only results, and optionally run a separately authorized guarded hardware trial.'
---

# Simulate TonyPi motions

Only use this procedure after the owner explicitly asks to learn/practice/calibrate, through the separate
`TONYPI_MCP_PROFILE=learning` stdio server (`docs/MOTION_LAYER.md`). Everyday requests use the ten high-level
library tools. A missing motion or body goal alone does not enable fine control; say what is missing instead.
Do not invoke hidden controls through shell/socket. New/changed candidates need calibrated-limit checks,
simulation and the library verification gate before everyday promotion; a save or hardware completion alone
is insufficient. Preserve model evidence separately from measured physical success.

Use the existing `tonypi` MCP server. `simulation_status({})` checks the pinned installation;
`simulation_catalog({})` discovers **all** upstream actions and current local motions/skills. IDs are
`upstream:wave`, `motion:wave`, `skill:wave`. Respect each entry's support reason. Scripted `get_up`, random
`dance`, head commands and other runtime behaviour cannot be inferred from a frame list.

Before drafting custom frames, follow [the existing-motion review](../../../docs/BODY_LEARNING.md#existing-motions-first):
inspect local/factory candidates and physical evidence, including failures. Simulate the closest supported action
first when simulation is requested or needed; model success does not establish real task success.

1. Call `simulate_motion({motion: "upstream:wave", render: true})`, or supply `frames` instead of `motion`:
   `[{ms: 500, pos: {"6": 575}}]`. Frames are Tony's motion space, with partial targets retained, servos 1..16,
   1..10000 ms (hardware >=20 ms), <=2000 frames and <=120 motion seconds. `count` is 1..5; native skill sequences
   repeat only `*` entries, exactly as tonyd does.
2. State the starting condition. `start` is `home` (upstream zero pose), `prone` or `supine` (upstream resting
   keyframes); `objective` is `stability` or `recover`. These do not position the real robot. To approximate Tony's
   present upright joints, read `get_joints({})` and pass its **raw**, complete 16-servo dictionary as `initial_raw`
   with `start: "home"`. The model is translated until the lowest foot sole touches its ideal floor. This assumes
   upright orientation and does not measure floor height or obstacles. Never call `look` during simulation-only
   work after STOP: it moves the head.
3. Inspect `simulation_success`, its definition, peak/end tilt, speed, spin, displacement, contacts and clipping
   counts. `image` is a local PNG of start/middle/end; use `view_image` to inspect it. `report_path` contains the
   full trace and clip details. A render error does not turn into a physics success. A failed or clipped simulation
   is useful evidence: edit the proposed frames and simulate again. Reports record exact inputs, calibration,
   pinned repository revision, adapter hash and MuJoCo version.
4. **Never automatically execute a simulated success.** A simulation-only request or a preceding STOP does not
   authorize physical movement. With current owner authorization for body work, inspect surroundings through the
   body-learning procedure and then call `simulation_execute({report_id: "..."})`. It requires an unclipped,
   successful upright stability report, unchanged motion/calibration, measured initial raw joints within 20 counts,
   tilt <=10 degrees, measured battery >=10.9 V and temperature <70 C. It never clears a latch. Native skill
   execution retains the skill name/count. All bridge trials are refused while tethered because arbitrary
   imported/custom actions lack reliable walking accounting. Do not bypass a refusal with `play_frames`.
5. Execution uses one existing Lab trial with a 30-degree guard, robot lock, preflight, stop/cancellation handling,
   recovery, stable finish and ledger. Read the returned physical trial; `hardware_validated` means only that this
   one trial finished without a detected fall. Use body-learning's 3–5 trials and compare noise before promotion.
   Cancellation produces no recovery or closing settle.

Only body channels 1..16 run in either backend. `.d6a` channels 17+ are explicitly reported and omitted, including
the head and grippers. Imported raw pulses are converted back to Tony's motion space by subtracting its trim;
local motions add trim once when simulated. Soft-limit and MuJoCo actuator clipping are reported separately.

The upstream ZERO/SIGN, geometry, mass, gains, friction and contact model belong to another TonyPi Pro, **not a
calibration of Tony**. XML left/right servo comments disagree with upstream Python; this adapter follows the
Python mapping (1..8 left, 9..16 right). Simulator metres are not phone measurements. No heat, battery, backlash,
torque-off or real obstacles are modeled. A pass is only a model result; no automatic promotion or safety claim.

Installation, reproducibility and evidence: [docs/SIMULATION.md](../../../docs/SIMULATION.md).
