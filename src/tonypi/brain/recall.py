"""Search Tony's history: an SQLite FTS5 index (BRAIN_DIR/history.sqlite) over the conversation log, the journal,
background task results and the learning ledger.

The index is brought up to date on every recall(): each source file is tracked by inode (so the voice service's
rotation conversation.jsonl -> conversation.old.jsonl doesn't re-index anything) plus a fingerprint of its first bytes,
and only bytes past the last complete line indexed are read. Malformed lines are skipped.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
from pathlib import Path

from tonypi.brain import LIVE_DIR, store

HEAD = 256  # bytes fingerprinted to notice a replaced file that reuses an inode
SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS docs USING fts5(text, source UNINDEXED, t UNINDEXED, ino UNINDEXED,
                                                   tokenize = 'porter unicode61');
CREATE TABLE IF NOT EXISTS files (ino TEXT PRIMARY KEY, path TEXT, offset INTEGER, head TEXT);
"""


def live_dir() -> Path:
    return Path(os.environ.get("TONYPI_LIVE_DIR") or LIVE_DIR)


def sources() -> list[tuple[str, Path]]:
    """(kind, path) of every file the index covers."""
    live, brain = live_dir(), store.root()
    out = [("talk", p) for p in sorted(live.glob("conversation*.jsonl")) if ".test" not in p.name]
    out += [("task", p) for p in sorted(live.glob("tasks*.jsonl"))]
    out += [("learn", p) for p in sorted((brain / "learning").glob("ledger*.jsonl"))]
    out += [("journal", p) for p in sorted((brain / "journal").glob("????-??-??.md"))]
    return out


# ---- line parsers: (t, text) or None -------------------------------------------------------------------------------

def _talk(line: str, _p: Path):
    d = json.loads(line)
    text = str(d.get("text") or "").strip()
    return (float(d.get("t") or 0), f"{d.get('role', '?')}: {text}") if text else None


def _task(line: str, _p: Path):
    d = json.loads(line)
    parts = [str(d.get(k) or "") for k in ("id", "status", "brief", "details", "lesson")]
    text = " | ".join(x for x in parts if x)
    return (float(d.get("end") or d.get("start") or d.get("t") or 0), text) if text else None


def _learn(line: str, _p: Path):
    d = json.loads(line)
    head = " ".join(str(d.get(k)) for k in ("exp", "exp_id", "label", "skill", "verdict") if d.get(k))
    rest = {k: v for k, v in d.items() if k in ("params", "metrics", "score", "objective", "note")}
    text = (head + " " + json.dumps(rest, separators=(",", ":"))[:600]).strip()
    return (float(d.get("t") or d.get("t_end") or 0), text) if text else None


JOURNAL_RE = re.compile(r"^- (\d\d):(\d\d) (.*)$")


def _journal(line: str, p: Path):
    m = JOURNAL_RE.match(line)
    if not m:
        return None
    day = time.strptime(p.stem, "%Y-%m-%d")
    t = time.mktime((day.tm_year, day.tm_mon, day.tm_mday, int(m[1]), int(m[2]), 0, 0, 0, -1))
    return t, m[3]


PARSERS = {"talk": _talk, "task": _task, "learn": _learn, "journal": _journal}


# ---- index ---------------------------------------------------------------------------------------------------------

def connect() -> sqlite3.Connection:
    db = sqlite3.connect(store.ensure() / "history.sqlite", timeout=10, isolation_level=None)
    db.executescript(SCHEMA)
    return db


def update(db: sqlite3.Connection | None = None) -> int:
    """Index whatever is new in the sources; returns the number of rows added."""
    own = db is None
    db = db or connect()
    added = 0
    try:
        db.execute("BEGIN IMMEDIATE")  # one indexer at a time across processes
        for kind, path in sources():
            try:
                st = path.stat()
                with path.open("rb") as f:
                    head = hashlib.sha1(f.read(HEAD)).hexdigest()
                    ino = f"{st.st_dev}:{st.st_ino}"
                    row = db.execute("SELECT offset, head FROM files WHERE ino=?", (ino,)).fetchone()
                    offset = 0
                    if row:
                        offset, old_head = row
                        # Same file grown past HEAD keeps its first bytes; a rewritten or truncated file doesn't.
                        if st.st_size < offset or (offset >= HEAD and old_head != head):
                            db.execute("DELETE FROM docs WHERE ino=?", (ino,))
                            offset = 0
                    if st.st_size == offset:
                        continue
                    f.seek(offset)
                    chunk = f.read()
            except OSError:
                continue
            end = chunk.rfind(b"\n") + 1  # only complete lines; a half-written one waits for the next update
            rows = []
            for raw in chunk[:end].decode(errors="replace").splitlines():
                if not raw.strip():
                    continue
                try:
                    got = PARSERS[kind](raw, path)
                except (ValueError, TypeError, AttributeError):
                    continue
                if got and got[1]:
                    rows.append((got[1], kind, got[0], ino))
            db.executemany("INSERT INTO docs (text, source, t, ino) VALUES (?, ?, ?, ?)", rows)
            db.execute("INSERT OR REPLACE INTO files VALUES (?, ?, ?, ?)", (ino, str(path), offset + end, head))
            added += len(rows)
        db.execute("COMMIT")
    except BaseException:
        if db.in_transaction:
            db.execute("ROLLBACK")
        raise
    finally:
        if own:
            db.close()
    return added


def _fts_query(query: str, joiner: str) -> str:
    words = re.findall(r"\w+", query.lower())
    return f" {joiner} ".join(f'"{w}"*' for w in words[:12])


def recall(query: str, days: float | None = None, limit: int = 8, source: str | None = None) -> list[dict]:
    """Best matches for `query` (all words first, any word if that finds nothing), newest first among equals:
    [{source, t, text}]. days limits to recent history; source to one of talk/task/learn/journal."""
    db = connect()
    try:
        update(db)
        since = time.time() - days * 86400 if days else 0
        for joiner in ("AND", "OR"):
            q = _fts_query(query, joiner)
            if not q:
                return []
            sql = ("SELECT source, t, text FROM docs WHERE docs MATCH ? AND t >= ?"
                   + (" AND source = ?" if source else "") + " ORDER BY bm25(docs), t DESC LIMIT ?")
            args = [q, since] + ([source] if source else []) + [int(limit)]
            rows = db.execute(sql, args).fetchall()
            if rows:
                return [{"source": s, "t": round(float(t or 0), 1), "text": text} for s, t, text in rows]
        return []
    finally:
        db.close()
