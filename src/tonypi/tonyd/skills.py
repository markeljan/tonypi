"""The skills tonyd runs: everything a brain (local LLM, Claude, Hermes, a script) may ask the body to do.

Names and descriptions only; the implementations are in body.py. A brain never sends joint targets.
"""

from __future__ import annotations

SKILLS: dict[str, str] = {
    "wave": "wave hello",
    "bow": "bow politely",
    "dance": "do a dance routine (about a minute)",
    "kung_fu": "kung fu moves",
    "twist": "twist the hips",
    "punch_left": "left uppercut",
    "punch_right": "right uppercut",
    "kick_left": "kick with the left foot",
    "kick_right": "kick with the right foot",
    "squat": "squat down and back up",
    "arms_up": "raise both arms",
    "flap_wings": "flap arms like wings",
    "step_in_place": "march in place (count = steps)",
    "walk_forward": "walk forward (count = steps)",
    "walk_backward": "walk backward (count = steps)",
    "turn_left": "turn left (count = steps)",
    "turn_right": "turn right (count = steps)",
    "step_left": "side-step left (count = steps)",
    "step_right": "side-step right (count = steps)",
    "look_left": "turn head left",
    "look_right": "turn head right",
    "look_up": "tilt head up",
    "look_down": "tilt head down",
    "look_center": "head straight",
    "nod": "nod yes",
    "shake_head": "shake head no",
    "lie_down": "lie down",
    "sleep": "go to sleep: lie down and switch the motors off",
    "get_up": "get up from lying, or wake up from sleep",
    "stand": "stand up straight",
    "settle": "back to a neutral stand after body moves (does nothing when lying or asleep)",
}

MAX_COUNT = 5
