"""Body learning: measured trials, a ledger, a body model and a skill library (docs/BODY_LEARNING.md).

gait.py transforms keyframes, metrics.py scores a trial from numbers, runner.py runs trials on the robot (through
tonyd only, holding the robot lock) and keeps the records in tonypi.brain.LEARN_DIR. phone.py is the optional outside
observer (the iPhone in companion mode). tools/learn.py and the tonypi-mcp tools (trial, compare, body_sweep, ledger,
body_model, library) are the front ends.
"""
