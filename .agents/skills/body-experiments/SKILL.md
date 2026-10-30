---
name: body-experiments
description: Plan, document, and cautiously test TonyPi joint movements or new body motions with live feedback.
---

Read `/home/pi/tonypi/docs/BODY_EXPERIMENTS.md` for setup, experiment steps and evidence conventions. Run `/home/pi/tonypi/tools/body_inventory.py` with `uv run python` for a read-only baseline and comparison. The recorded library extrema and EEPROM limits are not safe mechanical limits. Establish physical support, human observation and clearance before movement; if unknown, collect read-only evidence and ask for the setup. Use fresh state, cool servos, one slow incremental joint test at a time, a tilt guard, a stable return, and short asynchronous waits. Stop on user request, latch refusal, unexpected movement or strain; never bypass a latch or automatically resume. Record commanded targets separately from actual readbacks and physical observation. Cover the head separately and do not infer balance ability from isolated joint movement. Do not change soft limits or EEPROM without explicit approval.

For fast read-only feedback and timestamped JSONL records, use `/home/pi/tonypi/tools/body_feedback.py`; pass the recorded motion-space targets with `--targets` and a phase with `--label`. See the documentation for timing, calibration, sensor freshness and interpretation. Target errors compare the supplied command to a sequential readback and do not prove endpoint, clearance or safety.
