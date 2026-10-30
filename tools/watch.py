#!/usr/bin/env python3
"""Watch what Tony is doing, live: every Codex thread of the live brain (L, autonomy sessions, background tasks,
their subagents) and his journal, one line per step.

    uv run python tools/watch.py            # follow from now, with the last 10 minutes as backlog
    uv run python tools/watch.py --since 60 --full

Lines: `HH:MM:SS who  kind  text`. who = L (the brain behind the voice), auto (an autonomy session), task (a background
task), …/sub (a subagent of it). kinds: think (reasoning summary), tool (calls in one step), say (a message: L's
[FINAL] lines are spoken), done (turn finished), journal.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import time
from datetime import date, datetime
from pathlib import Path

SESSIONS = Path.home() / ".config/tonypi/codex-live/sessions"
LIVE = Path.home() / ".local/share/tonypi/live"
BRAIN = Path.home() / ".local/share/tonypi/brain"
COLORS = {"L": "36", "auto": "35", "task": "33", "journal": "32"}
TOOL_RE = re.compile(r"tools\.(?:mcp__(\w+?)__)?(\w+)\(")
CMD_RE = re.compile(r"cmd\s*:\s*[\"'`]([^\"'`]{0,160})")


def autonomy_ids() -> set[str]:
    out = subprocess.run(["journalctl", "-u", "tonypi-voice", "--since", "today", "-o", "cat", "--no-pager"],
                         capture_output=True, text=True).stdout
    return set(re.findall(r"autonomy session ([0-9a-f-]{36})", out))


class Follower:
    def __init__(self, full: bool, color: bool) -> None:
        self.full, self.color = full, color
        self.pos: dict[Path, int] = {}
        self.who: dict[str, str] = {}  # thread id -> label
        self.auto, self.auto_t = set(), 0.0
        self.batch: list[tuple[str, str, str, str]] = []  # one poll's lines, printed in time order

    def label(self, meta: dict) -> str:
        tid = meta.get("id", "")
        if tid in self.who:
            return self.who[tid]
        if time.time() - self.auto_t > 20:
            self.auto, self.auto_t = autonomy_ids(), time.time()
        try:
            l_thread = json.loads((LIVE / "state.json").read_text()).get("thread")
        except (OSError, ValueError):
            l_thread = None
        parent = meta.get("forked_from_id") or meta.get("parent_thread_id")
        if tid == l_thread:
            name = "L"
        elif tid in self.auto:
            name = "auto"
        elif parent and parent != tid:
            name = (self.who.get(parent) or ("auto" if parent in self.auto else "L" if parent == l_thread else "task")) + "/sub"
        else:
            name = "task"
        self.who[tid] = name
        return name

    def out(self, ts: str, who: str, kind: str, text: str) -> None:
        self.batch.append((ts, who, kind, text))

    def flush(self) -> None:
        self.batch.sort(key=lambda e: e[0])
        for e in self.batch:
            self.emit(*e)
        self.batch = []

    def emit(self, ts: str, who: str, kind: str, text: str) -> None:
        text = " ".join(text.split())
        if not self.full and len(text) > 220:
            text = text[:217] + "..."
        tag = f"{who:<9}"
        if self.color:
            tag = f"\x1b[{COLORS.get(who.split('/')[0], '37')}m{tag}\x1b[0m"
        print(f"{ts} {tag} {kind:<7} {text}", flush=True)

    def step(self, path: Path, line: str, who: str) -> None:
        try:
            j = json.loads(line)
        except ValueError:
            return
        p = j.get("payload") or {}
        ts = datetime.fromisoformat(j.get("timestamp", "1970-01-01T00:00:00Z").replace("Z", "+00:00")).astimezone().strftime("%H:%M:%S")
        t = p.get("type")
        if t == "reasoning":
            text = " ".join(s.get("text", "") for s in p.get("summary") or [])
            if text:
                self.out(ts, who, "think", text.replace("**", ""))
        elif t == "custom_tool_call":
            code = p.get("input", "")
            calls = [f"{srv + ':' if srv else ''}{name}" for srv, name in TOOL_RE.findall(code)]
            cmds = CMD_RE.findall(code)
            text = ", ".join(dict.fromkeys(calls)) or p.get("name", "")
            if cmds:
                text += "  $ " + " | ".join(cmds)
            self.out(ts, who, "tool", text)
        elif t == "function_call":
            args = p.get("arguments", "")
            if p.get("name") in ("send_message", "followup_task", "spawn_agent"):  # payloads are encrypted
                try:
                    a = json.loads(args)
                    args = " ".join(f"{k}={v}" for k, v in a.items() if k in ("target", "agent_type", "role", "nickname"))
                except ValueError:
                    args = ""
            self.out(ts, who, "tool", f"{p.get('name')} {args}")
        elif t == "message" and p.get("role") == "assistant":
            text = " ".join(c.get("text", "") for c in p.get("content") or [] if isinstance(c, dict))
            if text:
                self.out(ts, who, "say", text)
        elif t == "task_complete":
            self.out(ts, who, "done", "turn finished")

    def poll(self, since: float) -> None:
        for path in sorted(SESSIONS.glob("*/*/*/rollout-*.jsonl")):
            try:
                st = path.stat()
            except OSError:
                continue
            if path not in self.pos:
                if st.st_mtime < since:
                    continue
                self.pos[path] = 0
            if st.st_size <= self.pos[path]:
                continue
            with path.open() as f:
                f.seek(self.pos[path])
                data = f.read()
            done = data.rfind("\n") + 1  # only complete lines
            self.pos[path] += len(data[:done].encode())
            lines = data[:done].splitlines()
            if path not in self.who and lines:
                try:
                    first = json.loads(path.open().readline()).get("payload", {})
                except ValueError:
                    first = {}
                self.who[path] = self.label(first)  # type: ignore[index]
            who = self.who.get(path, "task")  # type: ignore[arg-type]
            for line in lines:
                self.step(path, line, who)

    def poll_journal(self, since: float) -> None:
        path = BRAIN / "journal" / f"{date.today():%Y-%m-%d}.md"
        if not path.exists():
            return
        first = path not in self.pos
        lines = path.read_text().splitlines()
        start = self.pos.get(path, 0)
        if first:
            cutoff = datetime.fromtimestamp(since).strftime("%H:%M")
            start = next((i for i, l in enumerate(lines) if l.startswith("- ") and l[2:7] >= cutoff), len(lines))
        for l in lines[start:]:
            if l.startswith("- "):
                self.out(l[2:7] + ":00", "journal", "", l[8:])
        self.pos[path] = len(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--since", type=float, default=10, help="backlog in minutes (default 10)")
    ap.add_argument("--full", action="store_true", help="don't shorten long lines")
    ap.add_argument("--no-color", action="store_true")
    ap.add_argument("--once", action="store_true", help="print the backlog and exit")
    a = ap.parse_args()
    f = Follower(a.full, not a.no_color)
    since = time.time() - a.since * 60
    while True:
        f.poll_journal(since)
        f.poll(since)
        f.flush()
        if a.once:
            return
        time.sleep(1)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
