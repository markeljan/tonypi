---
name: autonomy-session
description: 'How to run an autonomy session - the work I do on my own when nobody is talking (a goal from goals.json, measured trials, code self-review). Use in threads started by the autonomy scheduler, and when the owner asks how autonomy works or to pause it.'
---

# Autonomy session

The voice service starts a session after ~75 s of quiet: a new thread with one goal (title, success, next step, last
notes) set as its Codex goal and a token budget. The owner speaking ends it within 2 s (the body stops); the next
session picks up from the ledger.

## Start (under a minute)
1. Read the objective. `tony goal show <id>`, `tony recall "<goal words>" --days 7`, and for body goals
   `library()`, `ledger(...)`, `body_model()`.
2. Body check if the goal needs the body: `get_state` (battery >= 10.9 V, hottest < 70 C, no latch). The scheduler
   already checked the robot lock (no motion tools while someone tests); a trial refusing with "robot lock" means
   someone started testing: switch to non-body work. Tool calls: AGENTS.md "Calling tools".
3. Visible work: one short line first with `tony say "<what I'm about to try>"`.

## Work
- Body goals: follow `body-learning` (and `gait-tuning`, `motion-authoring`, `phone-observer`). Batches of trials,
  not single probes. Falls are data: the runner gets up and goes on.
- Non-body goals: code self-review, voice-pipeline fixes, memory curation; follow `self-upgrade`. Small tested
  commits (`auto:`), never safety code.
- After each meaningful step: `tony goal note <id> "<numbers>"`. Change approach after 2 failures (`when-stuck`).
- Notable result (a promoted gait, a goal done, a fix committed): `tony say` one short line and
  `tony inbox add "<the result>"`.

## Never
- Ask the owner about setup, clearance, observers or fixtures, or wait for an answer. If the phone is needed and not
  placed, `tony inbox add` the placement request and do work that needs no phone.
- Use `phone_say`, restart services the owner is using, change safety code, auth, network or boot.
- Move after a stop until the owner asks for a move.

## End
1. End in a stable pose: `run_skill("settle")` or `run_skill("lie_down")` if I'm going to stay put.
2. `tony goal note <id> "<where I stopped, next step>"`, `tony goal update <id> --next-step "..."`.
3. Success criteria met by measurement: `tony goal done <id> "<evidence>"`. Blocked: `tony goal block <id> "..."`.
4. `tony journal --kind autonomy "<one line summary>"`; `tony remember --kind lesson` for a lesson that transfers.

Owner control: "be quiet / stay still / pause" -> `tony autonomy pause [MIN]` (all) or `tony autonomy body-pause
[MIN]` (no movement); `tony autonomy resume`; `tony autonomy now` starts one at once; `tony autonomy status`.
