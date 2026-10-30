# TonyPi simulation integration

The repository Markel named is [pab47/TonyPi](https://github.com/pab47/TonyPi), pinned to
[`97807f5e26049ce1c3d90b1c80d5d2a92606d40f`](https://github.com/pab47/TonyPi/tree/97807f5e26049ce1c3d90b1c80d5d2a92606d40f).
It supplies an MIT-licensed TonyPi Pro MuJoCo model, 118 SQLite `.d6a` action groups and simulation/hardware
controllers. This integration reads its data and literal servo calibration; it does **not** import its Python
controllers or direct Board/MPU hardware backend. The external checkout retains its original MIT LICENSE
(copyright 2026 Pranav Bhounsule).

## Install and load

```sh
cd /home/pi/tonypi
uv sync --extra agent --extra simulation --extra voice --extra vision
uv run tonypi-sim install
# Optional software-rendering runtime; no display/GPU required:
sudo apt-get install -y --no-install-recommends libosmesa6
sudo systemctl restart tonypi-mcp
```

The optional simulation extra pins compatible MuJoCo 3.17.x and Mink 1.3.x. The installer clones only into
`~/.local/share/tonypi/simulation/PAB47-TonyPi` (override parent with `TONYPI_SIM_DIR`), checks out the fixed revision,
and refuses a different/modified existing checkout. It never executes upstream installation scripts or changes
robot calibration. Edited/untracked model/action/calibration assets are refused, rather than silently assigned
the pinned version. Reinstallation does not overwrite an existing checkout. No new service or network port is used.

Four new tools are served by the existing `tonypi` MCP server:

| Tool | Effect |
| --- | --- |
| `simulation_status` | Read installation, revision, dependencies and limits |
| `simulation_catalog` | List all upstream actions and local motions/skills, including unsupported reasons |
| `simulate_motion` | Headless physics and optional PNG; no body commands |
| `simulation_execute` | Explicit separate guarded hardware trial, subject to existing gates |

`simulation_execute` is in the voice harness's BODY_TOOLS list so no-body sessions hide it. The running voice
process must reload that classification at its next managed restart; background tasks never restart voice.
The new skill is `.agents/skills/tonypi-simulation/SKILL.md` and loads in new brain threads.

## Shell and MCP examples

```sh
uv run --extra simulation tonypi-sim status
uv run --extra simulation tonypi-sim catalog
uv run --extra simulation tonypi-sim run upstream:wave
uv run --extra simulation tonypi-sim run upstream:stand_up_back --start supine --objective recover
uv run --extra simulation tonypi-sim run upstream:tonypi_walk_forward --offline --no-render
uv run --extra simulation tonypi-sim run --frames /tmp/candidate.json --offline
```

`--offline` makes no tonyd connection and uses identity trim/limits. Such reports cannot be executed by the bridge.
The shell CLI deliberately has no hardware command. New keyframe candidates can be made and tested by supplying
JSON frames; the four upstream baked `tonypi_*` IK actions are ordinary discoverable `.d6a` inputs. Live Mink IK
generation, upstream interactive viewers and scripted runtime skills are not wrapped. Mink is installed for
future development, and its presence does not claim these controllers have been validated.

In MCP code mode:

```python
tools.mcp__tonypi__simulation_catalog({})
tools.mcp__tonypi__simulate_motion({"motion": "upstream:wave", "render": True})
# Or generate a candidate: {"frames": [{"ms": 500, "pos": {"6": 575}}]}
# Only in a later, physically authorized task:
tools.mcp__tonypi__simulation_execute({"report_id": "the returned id"})
```

## Interpretation and safeguards

Each simulation runs in a fresh process with a 90-second wall limit. A simulator-only resource lock limits
concurrency. Inputs are capped at 2000 frames and 120 motion seconds; hardware frames also require <=400 frames
and >=20 ms. Partial frames retain unspecified joint targets. Specified targets interpolate from the measured
simulated joint angle over each frame (4 ms physics timestep, rounded up to whole steps); the final targets remain
actively held during a default 1 s settling period. This approximates the upstream simulator contract, not the
actual servo dynamics or torque-off behavior.

Stability success means finite physics, final torso tilt <30 degrees, free-base speed <0.08 m/s, angular speed
<20 degrees/s, foot contact, and peak tilt <45 degrees. Recovery omits the peak-tilt condition because it starts
lying. This is a generic posture check: it does not prove a wave occurred, a kick hit, or a requested distance was
achieved. The report exposes displacement, yaw, contacts and traces for judging an action-specific goal. Peak
tilt >=60 degrees is reported even for a deliberately lying start; it is not labelled a newly observed fall.
Joint and soft-limit clipping do not get hidden behind a pass; any clipping blocks hardware candidate status.

Start `home` uses upstream's zero joints, not Tony's stand pose. Prone/supine use upstream keyframes. `initial_raw`
can set all 16 body pulses with home orientation and a computed foot-to-floor translation. None of these options
positions the real robot. A hardware trial requires actual raw pulses within 20 counts of the report start and
upright tilt <=10 degrees; otherwise simulate again from an appropriate measured pose. Calibration changes or
motion edits also invalidate the report. Head/gripper channels are omitted in both backends and listed explicitly.

Raw `.d6a` pulses map to model radians using upstream ZERO/SIGN and 240 degrees/1000 counts; those tables were
measured on a different robot. Native motion-space frames add Tony's trim and soft limits in the simulation;
imported raw frames subtract Tony's trim before tonyd adds it back. No EEPROM calibration or saved-motion overrides
are performed. The Python servo mapping conflicts with some XML left/right comments; this adapter follows the
Python convention and reports the uncertainty. Model masses, friction, torque limits, gains and geometry are not
fit to Tony, and real heat, battery, backlash, torque-off, floor and obstacles are absent. Results cannot establish
physical safety, success or automatic promotion.

Hardware execution never clears a stop latch. It uses the existing Lab runner's lock/preflight, one 30-degree
guarded measured trial, cancellation, recovery and stable finish, with a second initial-condition check immediately
before submission. Native skills are submitted by name so tonyd retains tether accounting. All bridge trials are refused while tethered because arbitrary imported or custom actions lack reliable
walking accounting. Do not bypass this refusal. Physical results enter the existing learning ledger; simulator results stay separate under
`~/.local/share/tonypi/simulation/reports/<id>/` (`request.json`, `physics.json`, `report.json`, optional `simulation.png`).

## Verification

Task1030-t6 made no physical moves and never cleared a latch. Tests use fake robot clients and cover SQLite
reading/order, partial frames, format limits, calibration, soft-limit clipping, native repeat semantics, scripted
skill exclusions, STOP/busy/lying/battery/temperature/tether refusal, stale inputs, initial pose checks, cancellation,
guarded Lab submission and stable completion. An optional installed-asset test actually runs headless MuJoCo.
The model's zero-pose hold produced peak/end tilt 1.26 degrees at 2 simulated seconds with no clipping; its PNG was
rendered through OSMesa and visually inspected. All 118 upstream actions ran without worker errors: 80 passed the generic posture check and 38 failed it;
clipping is reported separately, so this is not 80 validated skills. The validation manifest is
`~/.local/share/tonypi/simulation/validation-1030-t6.json`. Native stand and wave also ran through the bridge.
No physical execution was attempted. The final relevant test suite passed 147 tests.
