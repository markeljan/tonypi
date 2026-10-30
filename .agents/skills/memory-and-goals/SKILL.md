---
name: memory-and-goals
description: 'Keep my memory, journal, goals and inbox current with the `tony` CLI, and search my past with tony recall. Use when the owner states a fact or preference, sets or changes a goal, asks what I did or learned, or when a session needs context.'
---

# Memory and goals

Everything lives in `~/.local/share/tonypi/brain/` (private git repo). The `tony` CLI is the only writer; add `--json`
after the subcommand for machine output.

## Remember
- The owner states a fact or preference: `tony remember --kind owner "<fact>"` at once, then answer.
- Body facts (measured): `--kind body`. Places and objects: `--kind home`. Transferable lessons: `--kind lesson`.
  Anything else: plain `tony remember "<fact>"` (goes to ## Recent, oldest roll into the journal).
- One line, concrete, with numbers where there are numbers. No duplicates: `tony memory` first if unsure.
- MEMORY.md stays <= 4 KB. A curation pass may rewrite it by hand: keep the sections, merge duplicates, commit.

## Journal
`tony journal --kind talk|task|goal|autonomy|learn|fall|code|note "<text>"`: what happened, one line each. The voice
service journals tasks and sessions itself; I add learning results, falls, commits and decisions.

## Recall
`tony recall "<words>" [--days N] [--source talk|task|learn|journal]` before saying I don't know or asking the owner
to repeat. "What did you do today": `tony history --today`.

## Goals (`tony goal ...`)
- Owner asks for something lasting ("learn to dance", "keep the battery above X"): `tony goal add "<title>" --owner
  --success "<measurable>" --next-step "<first step>" [--body]`.
- Self goals start proposed: `tony goal add "<title>" --success ... --next-step ...` (no `--owner`); the owner can drop
  them.
- Progress: `tony goal note <id> "<numbers, what changed>"` after every meaningful step; `tony goal update <id>
  --next-step "..."`.
- Done only when the success criteria are met by measurement: `tony goal done <id> "<evidence: exp ids, commits>"`.
- Blocked after 2-3 different approaches: `tony goal block <id> "<blocker>"`. Dropped on request: `tony goal drop`.
- `tony goal list` shows open goals, owner goals first.

## Inbox
`tony inbox add "<thing to tell the owner>"` for news from autonomy work. When the owner starts talking and asks
what's new, `tony inbox take` and give the one or two items that matter.
