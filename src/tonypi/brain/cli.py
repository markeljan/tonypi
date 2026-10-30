"""`tony`: Tony's memory, goals, journal, recall and autonomy from the shell (for the brain's own threads and for us).

  tony memory                                   the snapshot a new thread gets
  tony remember [--kind K] TEXT                 K: fact (default), lesson, owner, body, home
  tony journal [--kind K] TEXT                  K: talk task goal autonomy learn fall code note
  tony goal list [--all] | show ID | add TITLE [...] | update ID [...] | note ID TEXT | done ID [EVIDENCE]
            | drop ID [WHY] | block ID REASON | activate ID | pause ID
  tony recall QUERY [--days N] [--limit N] [--source talk|task|learn|journal]
  tony inbox [add TEXT | take]
  tony say TEXT                                 speak through tonypi-voice
  tony autonomy status | wander [MIN] | stop | now | pause [MIN] | resume | body-pause [MIN]
  tony history [--today] [--days N]             journal lines
Add --json (after the subcommand) for machine output where it applies.
"""

from __future__ import annotations

import argparse
import json
import socket
import sys
import time

from tonypi.brain import store


def _ts(t: float) -> str:
    return time.strftime("%m-%d %H:%M", time.localtime(t)) if t else "-"


def _out(args, data, text: str) -> None:
    print(json.dumps(data, ensure_ascii=False) if getattr(args, "json", False) else text)


def _goal_detail(g: dict) -> str:
    lines = [store.goal_line(g)]
    for k in ("why", "success", "next_step", "blocked_on"):
        if g.get(k):
            lines.append(f"  {k}: {g[k]}")
    lines.append(f"  attempts: {g.get('attempts', 0)}  last_worked: {_ts(g.get('last_worked', 0))}"
                 + (f"  rung: {g['rung']}" if g.get("rung") else ""))
    if g.get("evidence"):
        lines.append("  evidence: " + "; ".join(map(str, g["evidence"])))
    lines += [f"  note {_ts(n.get('t', 0))}: {n.get('text', '')}" for n in g.get("notes") or []]
    return "\n".join(lines)


def cmd_goal(args) -> int:
    a = args.action
    if a == "list":
        goals = store.list_goals(None if args.all else ("active", "proposed", "paused", "blocked"))
        _out(args, goals, "\n".join(store.goal_line(g) for g in goals) or "no goals")
        return 0
    if a == "add":
        gid = store.add_goal(" ".join(args.text), why=args.why or "", origin="owner" if args.owner else "self",
                             priority=args.priority, success=args.success or "", next_step=args.next_step or "",
                             needs_body=args.body, **({"state": args.state} if args.state else {}))
        _out(args, {"id": gid}, gid)
        return 0
    if not args.text:
        print("goal id needed", file=sys.stderr)
        return 2
    gid, rest = args.text[0], " ".join(args.text[1:])
    if a == "show":
        g = store.get_goal(gid)
        if g is None:
            print(f"no goal {gid}", file=sys.stderr)
            return 1
        _out(args, g, _goal_detail(g))
        return 0
    if a == "note":
        g = store.note_goal(gid, rest)
    elif a == "update":
        fields = {k: getattr(args, k) for k in ("title", "why", "success", "next_step", "state", "priority",
                                                "blocked_on", "evidence") if getattr(args, k) is not None}
        if args.body is not None:
            fields["needs_body"] = args.body
        if not fields:
            print("nothing to update", file=sys.stderr)
            return 2
        g = store.update_goal(gid, **fields)
    else:
        state = {"done": "done", "drop": "dropped", "block": "blocked", "activate": "active", "pause": "paused"}[a]
        fields = {"state": state}
        if a == "block":
            fields["blocked_on"] = rest or "unspecified"
        elif rest and a == "done":
            fields["evidence"] = rest
        if rest and a in ("drop", "pause"):
            store.note_goal(gid, f"{a}: {rest}")
        g = store.update_goal(gid, **fields)
    _out(args, g, store.goal_line(g))
    return 0


def cmd_autonomy(args) -> int:
    now = time.time()
    a = args.action
    if a == "pause":
        store.set_autonomy(paused_until=now + 60 * (args.minutes or 60), run_now=False)
    elif a == "resume":
        store.set_autonomy(paused_until=0, body_paused_until=0)
    elif a == "now":
        store.set_autonomy(paused_until=0, run_now=True)
    elif a == "wander":
        store.set_autonomy(paused_until=0, run_now=True, wander_until=now + 60 * (args.minutes or store.WANDER_MIN))
    elif a == "stop":
        store.set_autonomy(run_now=False, wander_until=0, stop_at=now)
    elif a == "body-pause":
        store.set_autonomy(body_paused_until=now + 60 * (args.minutes or 30))
    st = store.autonomy_state()

    def left(k: str) -> int:
        return max(0, int((st.get(k, 0) - now) / 60))

    text = (f"wander: {left('wander_until')} min  paused: {left('paused_until')} min  body paused: {left('body_paused_until')} min  "
            f"run_now: {st['run_now']}  today: {st['sessions_today']} sessions, {st['tokens_today'] // 1000}k tokens  "
            f"last end: {_ts(st['last_session_end'])}")
    _out(args, st, text)
    return 0


def cmd_say(args) -> int:
    from tonypi.voice import SOCK

    text = " ".join(args.text).strip()
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(5)
            s.connect(SOCK)
            s.sendall((json.dumps({"op": "say", "text": text}) + "\n").encode())
            reply = s.makefile("r").readline()
    except OSError as e:
        print(f"voice not reachable: {e}", file=sys.stderr)
        return 1
    print(reply.strip() or "{}")
    return 0


def cmd_history(args) -> int:
    days = store.journal_days()
    days = [time.strftime("%Y-%m-%d")] if args.today else days[-max(1, args.days):]
    lines = []
    for d in days:
        got = store.journal_lines(d)
        if got:
            lines += [f"{d} {x[2:]}" for x in got]
    lines = lines[-args.limit:] if args.limit else lines
    _out(args, lines, "\n".join(lines) or "journal empty")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="tony", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    js = argparse.ArgumentParser(add_help=False)
    js.add_argument("--json", action="store_true")

    sub.add_parser("memory", help="print the memory snapshot").add_argument("--max-bytes", type=int, default=6000)
    r = sub.add_parser("remember", help="keep a fact in MEMORY.md")
    r.add_argument("--kind", default="fact")
    r.add_argument("text", nargs="+")
    j = sub.add_parser("journal", help="add a journal line")
    j.add_argument("--kind", default="note")
    j.add_argument("text", nargs="+")

    g = sub.add_parser("goal", parents=[js], help="the goal ledger")
    g.add_argument("action", choices=["list", "show", "add", "update", "note", "done", "drop", "block", "activate",
                                      "pause"])
    g.add_argument("text", nargs="*", help="TITLE for add; ID [TEXT] otherwise")
    g.add_argument("--all", action="store_true", help="list: include done and dropped")
    g.add_argument("--owner", action="store_true", help="add: the owner asked for it (starts active)")
    g.add_argument("--body", action=argparse.BooleanOptionalAction, default=None, help="needs the body")
    g.add_argument("--priority", type=int, default=None, help="1 (highest) to 5")
    g.add_argument("--state", choices=store.STATES)
    for opt in ("--title", "--why", "--success", "--next-step", "--blocked-on", "--evidence"):
        g.add_argument(opt)

    rc = sub.add_parser("recall", parents=[js], help="search conversations, journal, tasks, ledger")
    rc.add_argument("query", nargs="+")
    rc.add_argument("--days", type=float)
    rc.add_argument("--limit", type=int, default=8)
    rc.add_argument("--source", choices=["talk", "task", "learn", "journal"])

    ib = sub.add_parser("inbox", parents=[js], help="things to tell the owner")
    ib.add_argument("action", nargs="?", choices=["list", "add", "take"], default="list")
    ib.add_argument("text", nargs="*")

    sy = sub.add_parser("say", help="speak through the voice service")
    sy.add_argument("text", nargs="+")

    au = sub.add_parser("autonomy", parents=[js], help="the autonomy scheduler's switches")
    au.add_argument("action", nargs="?", choices=["status", "wander", "stop", "now", "pause", "resume", "body-pause"], default="status")
    au.add_argument("minutes", nargs="?", type=float)

    h = sub.add_parser("history", parents=[js], help="journal lines")
    h.add_argument("--today", action="store_true")
    h.add_argument("--days", type=int, default=2)
    h.add_argument("--limit", type=int, default=60)

    args = p.parse_args(argv)
    try:
        if args.cmd == "memory":
            print(store.memory_snapshot(args.max_bytes))
        elif args.cmd == "remember":
            print(store.remember(" ".join(args.text), args.kind))
        elif args.cmd == "journal":
            store.journal(args.kind, " ".join(args.text))
            print("ok")
        elif args.cmd == "goal":
            if args.action == "add" and args.priority is None:
                args.priority = 2 if args.owner else 3
            return cmd_goal(args)
        elif args.cmd == "recall":
            from tonypi.brain.recall import recall

            hits = recall(" ".join(args.query), days=args.days, limit=args.limit, source=args.source)
            _out(args, hits, "\n".join(f"{_ts(h['t'])} [{h['source']}] {h['text'][:300]}" for h in hits)
                 or "no matches")
        elif args.cmd == "inbox":
            if args.action == "add":
                n = store.inbox_add(" ".join(args.text))
                _out(args, {"queued": n}, f"{n} queued")
            else:
                items = store.inbox_take() if args.action == "take" else store.inbox_list()
                _out(args, items, "\n".join(f"- {x}" for x in items) or "inbox empty")
        elif args.cmd == "say":
            return cmd_say(args)
        elif args.cmd == "autonomy":
            return cmd_autonomy(args)
        elif args.cmd == "history":
            return cmd_history(args)
    except (KeyError, ValueError) as e:
        print(f"error: {e.args[0] if e.args else e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
