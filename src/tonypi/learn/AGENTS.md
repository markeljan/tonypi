# Body learning runtime

Measured trials on the real robot, the ledger and the body model. Procedure: `docs/BODY_LEARNING.md` and the skill
`body-learning`. Front ends: the `tonypi` MCP tools (`trial`, `compare`, `body_sweep`, `ledger`, `body_model`,
`library`, `get_job`) and `uv run python tools/learn.py` with the same verbs.

## Files
- `runner.py` `Lab`: preflight (idle, no latch, battery >= 10.9 V, hottest < 70 C; lying -> get_up), settle, phone
  recording in companion mode, the move with a tilt guard, score, one ledger line. A fall is data: get_up (max twice,
  then the batch stops). `compare` interleaves base and candidate and gives a verdict; `sweep` maps joints.
- `gait.py` pure keyframe transforms over a base motion: `time_scale`, `amplitude` (around the stand pose), `offset`
  {servo: delta}, `mirror` (left <-> right), `repeat`/concat. Candidates are a base plus these params.
- `metrics.py` scores per objective (walk, turn, balance, pose, generic). fell = lying or end tilt > 45 deg.
- `phone.py` optional client for the phone MCP (record, track, episode). No phone -> metrics without it.

## Records (`~/.local/share/tonypi/brain/learning/`)
- `ledger.jsonl` one line per trial or swept joint: exp id, label, skill/candidate hash, params, metrics, score,
  job id, phone episode id, verdict.
- `library.json` per skill key: candidates (n, mean, std, falls), incumbent, best, evidence.
- `body_model.json` per servo: sign, range, speed, error, IMU response.

## Rules
- Moves go through `TonydClient` only, so stop and the temperature guard always apply.
- The batch holds `/run/lock/tonypi-robot.lock` non-blocking: held by someone else -> refuse with a clear message.
- MCP calls return within ~200 s; rerun for more trials.
- Tests: `uv run pytest -q tests/test_learn.py` (fake tonyd and phone; never the real robot).
