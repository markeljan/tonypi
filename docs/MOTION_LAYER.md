# Motion library and MCP profiles

Everyday requests select named library actions; the model does not generate joint angles. tonyd alone owns
servos/camera and reflexes. The HTTP MCP service defaults to the everyday profile, with ten tools:

| Tool | Arguments / purpose |
|---|---|
| `move` | `direction`: forward, backward, turn_left, turn_right, side_left, side_right; `steps=1`, `wait=true` |
| `posture` | `name`: stand, squat, rest; `wait=true` |
| `get_up` | `wait=true`; choose recovery from IMU orientation |
| `gesture` | known catalog `name`, `wait=true` |
| `look` | `pan_deg=0`, `tilt_deg=0` within head limits |
| `perceive` | `mode`: snapshot (default) or context |
| `say` | `text` |
| `stop` | stop and latch without motion; never clears the latch |
| `status` | body/job state and curated catalog names/availability |
| `run_skill` | approved composite `skill`, `count=1`, `wait=true` |

Unknown names and invalid counts/arguments fail. A requested distance or turn angle needs measured calibration;
step count is not distance. If no available action fits, Tony says so and waits for an owner request to learn,
practice or calibrate. He does not route around missing tools with shell/socket calls or an alternate hardware API.
A body task or autonomy goal by itself does not enable fine control.

## Explicit learning profile

After the owner asks to learn/practice/calibrate, use a separate stdio MCP process:

```sh
cd /home/pi/tonypi
TONYPI_MCP_PROFILE=learning uv run tonypi-mcp --transport stdio
```

Do not add that server to the everyday Codex configuration. End the learning session by closing its client/process;
HTTP everyday service remains separate. Learning adds `practice_skill` for experimental/legacy skill execution with an optional custom tilt guard.
`run_skill` keeps the same curated-only contract and `skill/count/wait` schema in both profiles.
Learning retains joint/frame/torque authoring, calibration, trials,
comparison, body sweeps, detailed inspection, simulation and evidence-gated `promote_skill` tools. It does not bypass tonyd or clear a stop latch.
EEPROM calibration and safety-code changes still require their specific owner authorization.

## Evidence and promotion

Keep provenance (source revision, license, original action name), preconditions, duration, calibration/servo limits
and safety metadata with a motion. Imported or saved frames are candidates, not automatically everyday actions.
Reject unknown channels, invalid frames, limit clipping and stale calibration/content evidence. Validate exact
content through kinematic/servo-limit checks and simulation before library verification. Composite skills must
resolve entirely to allowed verified actions; saving a composite cannot launder an unverified motion.

Report evidence separately: source available, model verification passed, hardware action completed, measured task
success. A stable simulator run does not prove a wave, kick, distance, obstacle crossing or real-robot reliability.
The audit entrypoint is `uv run python tools/audit_motion_library.py`; it reads calibration and runs model-only
experiments, never physical moves. Inspect the source/license findings in `docs/MOTION_SOURCES.md` and exact
per-action evidence in `data/motion_catalog.json`; a raw asset's presence does not make its action available.
For an owner-authorized learned candidate, after the verification and measured-trial gates are satisfied,
`uv run python tools/promote_motion.py <name> <simulation-report-id>` records the promoted exact frame sequence.
It never moves the body and refuses missing/stale/insufficient evidence. Promotion requires at least five
exact-frame, calibration/start-matched, no-fall job and ledger records. Promoted sequences support count 1 only
and are refused while tethered until their walking accounting is verified. This is not an everyday MCP tool.

Physical acceptance is supervised and records exact motion/calibration hashes, job IDs, conditions and failures.
An improved motion additionally follows the repeated comparison criteria in `BODY_LEARNING.md` before promotion.
The idle-only `tools/archive_motion_candidates.py --apply` moves legacy user motions/skills to a timestamped archive
with checksums and restore instructions. Archive superseded user motions/skills under `~/.local/share/tonypi` with an inventory and restore path; never delete
them as cleanup or silently overwrite a user-authored override.

Fall detection → get-up → resume is a tonyd responsibility. It must preserve stop/cancel, battery, thermal,
preconditions and job bounds. If recovery cannot run, report failure and remain stopped; an LLM must not improvise
joint-level recovery. The short tool schema and compact status/observation support a future small offline selector;
this rework does not add an offline runtime.

## Supervised real-robot acceptance (owner runs later)

No physical motion is part of this rework's deployment verification. Leave the deliberately muted voice stopped.

The checklist is conditional: run only actions currently available in `status`. Model failures and clipping
remain blocked; supervised acceptance does not bypass that gate. Unavailable recovery/bow/kick/squat/rest actions
need an explicitly authorized learning investigation and fresh validation first. Forward and small-step turn
variants have model evidence only; their physical distance, angle and reliability still require measurement.

1. Read `status`: idle, no active job, battery at least 10.9 V, hottest servo below 70 C; inspect catalog evidence.
   Check tool discovery: exactly ten everyday tools; the separate learning client exposes fine tools.
2. Call `stop`, then `status`; confirm latch and no motion. Invalid names/counts and unverified composites must fail
   without starting a body job. The owner explicitly requests movement before continuing past a latch.
3. Run available stand, wave and bow individually from their documented starting posture; record jobs, peak/end tilt and video.
4. Run available one-step forward/backward, turn and side-step actions separately. Measure gyro yaw and phone displacement;
   do not derive distance from step count or screenshots. Check stable finish and stop response between jobs.
5. If model verified and available, run squat/rest, then front/back recovery under owner supervision from each documented start. Confirm orientation
   choice and stable finish. Test recovery/resume only with a short job and confirm stop cancels pending recovery.
6. Only after applicable prior checks pass, test available kicks and composites individually. Record failures and withhold affected actions;
   repeat reliability trials before claiming them proven. Leave voice stopped until the owner chooses to unmute it.
