---
name: improve-myself
description: When told "learn X", "improve Y", "fix how you do Z", or after a failure you understood: change your own skills, SOUL or harness code, test, commit, reload.
version: 1.0.0
metadata:
  hermes:
    tags: [robot, self-update, learning]
    category: robot
---

# Improve myself

What to change (smallest that fixes it):
- A way of doing a kind of task (how to walk to things, find things, make moves): a skill. `skill_manage` to edit or
  create it (category robot), or `self_write("hermes/skills/robot/NAME/SKILL.md", ...)`.
- A move: a better motion/skill in tonyd (`save_motion` / `save_skill`, NAME_hermes): see tune-motion / new-motion.
- How you behave in general (rules, priorities): `self_read("hermes/SOUL.md")`, edit, `self_write`. Keep it short.
- A fact about the home: memory.
- The harness itself (src/tonypi/hermes/*.py: front end, keeper, tools): only when a skill or SOUL change can't fix
  it. `self_read` the file, `self_write` the whole new version, `self_test` (must pass), then `self_reload("front")`.

Then:
1. `self_test()` after any code change; fix until it passes.
2. `self_commit("what changed and why")`: commits on your branch (skills you edited with skill_manage are included).
3. `self_reload("persona")` for SOUL/skills (applies now), `"front"` for harness code (after this task), `"gateway"`
   for hermes/config.yaml (after this task).
4. If you were asked to re-do a task with the improvement, do it now, then `task_done`.
Say in one sentence what you changed.
