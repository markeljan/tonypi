"""Tony's long-term memory on disk: MEMORY.md, the goal ledger, the daily journal, the inbox and the autonomy state.

Everything lives in BRAIN_DIR (a local git repo, never pushed: every memory edit can be rolled back). Writers are the
voice service, autonomy sessions and the `tony` CLI, possibly at the same time, so every write holds an fcntl lock on
BRAIN_DIR/.lock and replaces files atomically. ensure() creates and seeds the directory on first use.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import subprocess
import threading
import time
from pathlib import Path

from tonypi.brain import BRAIN_DIR

MEMORY_MAX = 4096  # bytes of MEMORY.md; typed facts that don't fit go to ## Recent
RECENT_MAX = 10  # lines in ## Recent; older ones are already in the journal
NOTES_MAX = 10  # notes kept per goal (the journal has them all)
SECTIONS = {"owner": "Owner", "body": "Body", "home": "Home", "lesson": "Lessons"}
STATES = ("proposed", "active", "paused", "blocked", "done", "dropped")
OPEN = ("proposed", "active", "paused", "blocked")
GOAL_FIELDS = {"title", "why", "origin", "priority", "state", "success", "next_step", "notes", "attempts",
               "last_worked", "blocked_on", "evidence", "needs_body", "rung", "kind", "tier", "created", "closed"}
KINDS = ("talk", "task", "goal", "autonomy", "learn", "fall", "code", "note")
WANDER_MIN = 30  # `tony autonomy wander` without minutes
AUTONOMY_DEFAULTS = {"paused_until": 0, "body_paused_until": 0, "run_now": False, "wander_until": 0, "stop_at": 0,
                     "day": "", "tokens_today": 0,
                     "sessions_today": 0, "last_session_end": 0, "goal_sessions": {}}


def root() -> Path:
    """BRAIN_DIR, re-read so tests can set TONYPI_BRAIN_DIR (or monkeypatch BRAIN_DIR) after import."""
    return Path(os.environ.get("TONYPI_BRAIN_DIR") or BRAIN_DIR)


# ---- locking, files, git -------------------------------------------------------------------------------------------

_rlock = threading.RLock()
_depth = 0


@contextlib.contextmanager
def locked():
    """Exclusive lock for read-modify-write; re-entrant within the process (flock on a second fd would deadlock)."""
    global _depth
    with _rlock:
        if _depth:
            _depth += 1
            try:
                yield
            finally:
                _depth -= 1
            return
        ensure()
        with open(root() / ".lock", "a") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            _depth = 1
            try:
                yield
            finally:
                _depth = 0
                fcntl.flock(f, fcntl.LOCK_UN)


def _write(path: Path, text: str) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def _write_json(path: Path, data) -> None:
    _write(path, json.dumps(data, indent=1, ensure_ascii=False) + "\n")


def _git(*args: str) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(["git", "-c", "user.name=Tony", "-c", "user.email=tony@tonypi.local", *args],
                              cwd=root(), capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None


def commit(msg: str) -> bool:
    """Commit everything changed in the brain repo (best effort; False when nothing changed or git failed)."""
    with locked():
        _git("add", "-A")
        r = _git("commit", "-q", "--no-verify", "-m", msg)
        return bool(r and r.returncode == 0)


_ensured: set[str] = set()


def ensure() -> Path:
    """Create BRAIN_DIR with its seed files and git repo (idempotent, cheap after the first call per directory)."""
    d = root()
    if str(d) in _ensured and (d / "goals.json").exists():
        return d
    for sub in ("", "journal", "learning"):
        (d / sub).mkdir(parents=True, exist_ok=True)
    seeded = []
    for name, text in (("MEMORY.md", SEED_MEMORY), ("inbox.md", "# Inbox: tell the owner next time he talks\n"),
                       (".gitignore", "*.sqlite*\n.lock\n.*.tmp\n")):
        if not (d / name).exists():
            _write(d / name, text)
            seeded.append(name)
    for name, data in (("goals.json", {"next": len(SEED_GOALS) + 1, "goals": _seed_goals()}),
                       ("autonomy.json", dict(AUTONOMY_DEFAULTS)), ("curriculum.json", SEED_CURRICULUM)):
        if not (d / name).exists():
            _write_json(d / name, data)
            seeded.append(name)
    if not (d / ".git").exists():
        _git("init", "-q")
    _ensured.add(str(d))
    if seeded:
        _git("add", "-A")
        _git("commit", "-q", "--no-verify", "-m", "seed: " + ", ".join(seeded))
    return d


# ---- journal -------------------------------------------------------------------------------------------------------

def _oneline(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def journal(kind: str, text: str, t: float | None = None) -> None:
    """Append "- HH:MM [kind] text" to journal/YYYY-MM-DD.md (local time)."""
    text = _oneline(text)
    if not text:
        return
    kind = re.sub(r"\W", "", str(kind or "note").lower()) or "note"
    lt = time.localtime(t or time.time())
    path = ensure() / "journal" / time.strftime("%Y-%m-%d.md", lt)
    with locked():
        new = not path.exists()
        with path.open("a") as f:
            if new:
                f.write(time.strftime("# Journal %Y-%m-%d\n\n", lt))
            f.write(f"- {time.strftime('%H:%M', lt)} [{kind}] {text}\n")


def journal_lines(day: str | None = None, n: int | None = None) -> list[str]:
    """The "- HH:MM [kind] ..." lines of one day (default today), the last n if given."""
    day = day or time.strftime("%Y-%m-%d")
    try:
        lines = [x for x in (root() / "journal" / f"{day}.md").read_text().splitlines() if x.startswith("- ")]
    except OSError:
        return []
    return lines[-n:] if n else lines


def journal_days() -> list[str]:
    return sorted(p.stem for p in (root() / "journal").glob("????-??-??.md"))


# ---- MEMORY.md -----------------------------------------------------------------------------------------------------

def _split(text: str) -> list[list[str]]:
    """[[header_or_preamble, line, ...], ...]: block 0 is everything before the first "## " header."""
    blocks: list[list[str]] = [[""]]
    for line in text.splitlines():
        if line.startswith("## "):
            blocks.append([line])
        else:
            blocks[-1].append(line)
    return blocks


def _join(blocks: list[list[str]]) -> str:
    out = []
    for b in blocks:
        body = b[1:]
        while body and not body[-1].strip():
            body.pop()
        out.append("\n".join(([b[0]] if b[0] else []) + body))
    return "\n\n".join(x for x in out if x).rstrip() + "\n"


def _block(blocks: list[list[str]], name: str) -> list[str]:
    for b in blocks:
        if b[0].strip().lower() == f"## {name.lower()}":
            return b
    blocks.append([f"## {name}"])
    return blocks[-1]


def remember(text: str, kind: str = "fact") -> str:
    """Keep a fact. Kinds owner/body/home/lesson go under their MEMORY.md section while it stays under MEMORY_MAX,
    anything else (or what doesn't fit) into ## Recent (last RECENT_MAX lines). Always journaled, so nothing is lost
    when Recent overflows. Returns where it went: the section name, or "known" for a duplicate."""
    text = _oneline(text)
    if not text:
        raise ValueError("nothing to remember")
    kind = (kind or "fact").lower()
    journal("learn" if kind == "lesson" else "note", f"remember ({kind}): {text}")
    path = ensure() / "MEMORY.md"
    with locked():
        mem = path.read_text()
        if re.search(r"(?m)^- (?:\d\d-\d\d \d\d:\d\d )?" + re.escape(text) + r"\s*$", mem):
            return "known"
        blocks = _split(mem)
        where = "Recent"
        if kind in SECTIONS:
            blocks2 = _split(mem)
            _block(blocks2, SECTIONS[kind]).append(f"- {text}")
            if len(_join(blocks2).encode()) <= MEMORY_MAX:
                blocks, where = blocks2, SECTIONS[kind]
        if where == "Recent":
            recent = _block(blocks, "Recent")
            lines = [x for x in recent[1:] if x.strip()] + [f"- {time.strftime('%m-%d %H:%M')} {text}"]
            recent[1:] = lines[-RECENT_MAX:]
        _write(path, _join(blocks))
        commit(f"remember ({kind}): {text[:60]}")
    return where


def memory_text() -> str:
    try:
        return (ensure() / "MEMORY.md").read_text()
    except OSError:
        return ""


# ---- goals ---------------------------------------------------------------------------------------------------------

def _load() -> dict:
    data = _read_json(ensure() / "goals.json", None)
    if not isinstance(data, dict) or not isinstance(data.get("goals"), list):
        data = {"next": 1, "goals": []}
    return data


def _save(data: dict) -> None:
    _write_json(root() / "goals.json", data)


def list_goals(states=None) -> list[dict]:
    """All goals (or those in `states`, a state name or a list), in ledger order."""
    if isinstance(states, str):
        states = [states]
    return [g for g in _load()["goals"] if not states or g.get("state") in states]


def get_goal(gid: str) -> dict | None:
    gid = _gid(gid)
    return next((g for g in _load()["goals"] if g.get("id") == gid), None)


def _gid(gid) -> str:
    gid = str(gid).strip()
    return gid if gid.startswith("g") else f"g{gid}"


def _check(fields: dict) -> None:
    bad = set(fields) - GOAL_FIELDS
    if bad:
        raise ValueError(f"unknown goal field(s): {', '.join(sorted(bad))}")
    if "state" in fields and fields["state"] not in STATES:
        raise ValueError(f"state must be one of {', '.join(STATES)}")
    if "priority" in fields:
        fields["priority"] = min(5, max(1, int(fields["priority"])))
    if "origin" in fields and fields["origin"] not in ("owner", "self"):
        raise ValueError("origin must be owner or self")


def add_goal(title: str, why: str = "", origin: str = "self", priority: int = 3, success: str = "",
             next_step: str = "", needs_body: bool = False, state: str | None = None, **extra) -> str:
    """Add a goal and return its id ("g12"). Owner goals start active, self goals proposed. An open goal with the same
    title is returned instead of a duplicate."""
    title = _oneline(title)
    if not title:
        raise ValueError("a goal needs a title")
    fields = {"why": _oneline(why), "origin": origin, "priority": priority, "success": _oneline(success),
              "next_step": _oneline(next_step), "needs_body": bool(needs_body),
              "state": state or ("active" if origin == "owner" else "proposed"), **extra}
    _check(fields)
    with locked():
        data = _load()
        for g in data["goals"]:
            if g.get("state") in OPEN and g.get("title", "").lower() == title.lower():
                return g["id"]
        gid = f"g{data['next']}"
        data["next"] += 1
        data["goals"].append(_goal(gid, title, **fields))
        _save(data)
        journal("goal", f"new {gid} ({fields['origin']}, p{fields['priority']}, {fields['state']}): {title}")
        commit(f"goal {gid}: new")
    return gid


def _goal(gid: str, title: str, **fields) -> dict:
    g = {"id": gid, "title": title, "why": "", "origin": "self", "priority": 3, "state": "proposed", "success": "",
         "next_step": "", "notes": [], "attempts": 0, "last_worked": 0, "blocked_on": "", "evidence": [],
         "needs_body": False, "created": round(time.time())}
    g.update(fields)
    return g


def update_goal(gid: str, **fields) -> dict:
    """Change goal fields (see GOAL_FIELDS; `evidence` given as a string is appended). Returns the goal. Closing a
    goal that carries a curriculum `rung` marks that rung done."""
    _check(fields)
    gid = _gid(gid)
    with locked():
        data = _load()
        g = next((x for x in data["goals"] if x.get("id") == gid), None)
        if g is None:
            raise KeyError(f"no goal {gid}")
        if isinstance(fields.get("evidence"), str):
            fields["evidence"] = (g.get("evidence") or []) + [fields["evidence"]]
        old = g.get("state")
        g.update(fields)
        if g.get("state") in ("done", "dropped") and old not in ("done", "dropped"):
            g["closed"] = round(time.time())
        _save(data)
        if g.get("state") != old:
            why = f": {g['blocked_on']}" if g["state"] == "blocked" and g.get("blocked_on") else ""
            journal("goal", f"{gid} {old} -> {g['state']}{why} ({g['title']})")
            if g["state"] == "done" and g.get("rung"):
                _rung_done(g["rung"], gid)
        commit(f"goal {gid}: " + ", ".join(sorted(fields)))
    return g


def note_goal(gid: str, text: str) -> dict:
    """Add a progress note (last NOTES_MAX kept; all of them go to the journal) and stamp last_worked."""
    text = _oneline(text)
    gid = _gid(gid)
    with locked():
        data = _load()
        g = next((x for x in data["goals"] if x.get("id") == gid), None)
        if g is None:
            raise KeyError(f"no goal {gid}")
        now = round(time.time())
        g["notes"] = ((g.get("notes") or []) + [{"t": now, "text": text}])[-NOTES_MAX:]
        g["last_worked"] = now
        _save(data)
        journal("goal", f"{gid}: {text}")
    return g


def next_goal(body_ok: bool, exclude=()) -> dict | None:
    """What to work on: owner active goals by priority, then self active, then self proposed. Goals marked
    needs_body are skipped when the body can't be used; ids in `exclude` are skipped."""
    order = [("owner", "active"), ("self", "active"), ("self", "proposed")]
    goals = [g for g in list_goals(("active", "proposed")) if g.get("id") not in exclude
             and (body_ok or not g.get("needs_body")) and (g.get("origin", "self"), g.get("state")) in order]
    if not goals:
        return None
    return min(goals, key=lambda g: (order.index((g.get("origin", "self"), g["state"])), g.get("priority", 3),
                                     _num(g["id"])))


def _num(gid: str) -> int:
    try:
        return int(gid.lstrip("g"))
    except ValueError:
        return 1 << 30


def goal_line(g: dict) -> str:
    """One-line digest: "g3 [active owner p2 body] Walk 1 m ... | next: ...". """
    tags = " ".join(x for x in (g.get("state"), g.get("origin"), f"p{g.get('priority', 3)}",
                                "body" if g.get("needs_body") else "") if x)
    s = f"{g['id']} [{tags}] {g.get('title', '')}"
    if g.get("state") == "blocked" and g.get("blocked_on"):
        s += f" | blocked on: {g['blocked_on']}"
    elif g.get("next_step"):
        s += f" | next: {g['next_step']}"
    return s


# ---- curriculum ----------------------------------------------------------------------------------------------------

def curriculum() -> list[dict]:
    data = _read_json(ensure() / "curriculum.json", SEED_CURRICULUM)
    return data.get("rungs", []) if isinstance(data, dict) else []


def next_rung(body_ok: bool) -> dict | None:
    """First rung not done whose prerequisites are done and that has no open goal yet."""
    rungs = curriculum()
    done = {r["id"] for r in rungs if r.get("done")}
    taken = {g.get("rung") for g in list_goals(OPEN)}
    for r in rungs:
        if (not r.get("done") and r["id"] not in taken and set(r.get("prereq", [])) <= done
                and (body_ok or not r.get("needs_body", True))):
            return r
    return None


def _rung_done(rid: str, gid: str) -> None:
    path = root() / "curriculum.json"
    data = _read_json(path, SEED_CURRICULUM)
    for r in data.get("rungs", []):
        if r["id"] == rid and not r.get("done"):
            r["done"], r["done_by"], r["done_t"] = True, gid, round(time.time())
            _write_json(path, data)
            journal("learn", f"curriculum rung {rid} done ({gid})")


# ---- inbox ---------------------------------------------------------------------------------------------------------

def _inbox_items(text: str) -> list[str]:
    return [x[2:].strip() for x in text.splitlines() if x.startswith("- ")]


def inbox_add(text: str) -> int:
    """Queue something to tell the owner next time he talks; returns the number of queued items."""
    text = _oneline(text)
    path = ensure() / "inbox.md"
    with locked():
        cur = path.read_text() if path.exists() else "# Inbox\n"
        if text and text not in _inbox_items(cur):
            cur = cur.rstrip("\n") + f"\n- {text}\n"
            _write(path, cur)
        return len(_inbox_items(cur))


def inbox_list() -> list[str]:
    try:
        return _inbox_items((ensure() / "inbox.md").read_text())
    except OSError:
        return []


def inbox_take() -> list[str]:
    """Return and clear the inbox."""
    path = ensure() / "inbox.md"
    with locked():
        text = path.read_text() if path.exists() else ""
        items = _inbox_items(text)
        if items:
            _write(path, "\n".join(x for x in text.splitlines() if not x.startswith("- ")).rstrip("\n") + "\n")
    return items


# ---- autonomy state ------------------------------------------------------------------------------------------------

def _today() -> str:
    return time.strftime("%Y-%m-%d")


def autonomy_state() -> dict:
    """autonomy.json with defaults; the per-day counters read as zero on a new day."""
    st = {**AUTONOMY_DEFAULTS, **_read_json(ensure() / "autonomy.json", {})}
    if st.get("day") != _today():
        st.update(day=_today(), tokens_today=0, sessions_today=0, goal_sessions={})
    return st


def set_autonomy(**kw) -> dict:
    """Merge fields into autonomy.json (unknown keys allowed, for the scheduler's own bookkeeping); returns it."""
    with locked():
        st = autonomy_state()
        st.update(kw)
        _write_json(root() / "autonomy.json", st)
    return st


# ---- snapshot ------------------------------------------------------------------------------------------------------

def memory_snapshot(max_bytes: int = 6000) -> str:
    """What a fresh thread should know: MEMORY.md, the top open goals, the inbox count and today's last journal
    lines, within max_bytes (MEMORY.md is cut first)."""
    goals = sorted(list_goals(("active", "proposed")),
                   key=lambda g: (g.get("origin") != "owner", g.get("priority", 3), g["state"] != "active",
                                  _num(g["id"])))
    tail = ["## Goals (tony goal list)"] + [f"- {goal_line(g)}" for g in goals[:6]]
    if len(goals) > 6:
        tail.append(f"- (+{len(goals) - 6} more)")
    n = len(inbox_list())
    if n:
        tail.append(f"\nInbox: {n} item(s) to tell the owner (tony inbox).")
    today = journal_lines(n=8)
    if today:
        tail += ["", "## Today (journal)"] + today
    tail_s = "\n".join(tail)
    room = max_bytes - len(tail_s.encode()) - 2
    mem = memory_text().strip()
    if len(mem.encode()) > room:
        mem = mem.encode()[:max(0, room - 20)].decode(errors="ignore").rstrip() + "\n[...cut]"
    return (mem + "\n\n" + tail_s).strip()[:max_bytes] if mem else tail_s[:max_bytes]


# ---- seeds ---------------------------------------------------------------------------------------------------------

SEED_MEMORY = """# Tony: memory (curated, <= 4 KB; details live in journal/, goals.json, learning/)

## Who I am
- I am Tony, a self-thinking, self-improving TonyPi humanoid (16 bus servos, head camera, IMU, Raspberry Pi 5).
- I control my body, upgrade my own code (/home/pi/tonypi: tests, small commits), use the owner's iPhone (camera, LiDAR,
  detection, on-phone models; companion mode = phone on a stand watching me, my main way to learn movement), sensors and AI tools.
- When nobody talks to me I work on my goals, learn by measured trials and keep memory, journal and goals current (`tony`).

## Owner
- Markel. Hates slowness and filler: act, then report the outcome in one short line.
- "Don't ask, figure it out": never ask about setup, clearance, observers or fixtures; verify with sensors (IMU tilt/yaw,
  joints, head camera looking down at the feet at tilt -40, the phone).
- Ask him only for irreversible actions (reboot, EEPROM servo_calibrate, deleting user data, auth, network/boot), changes
  to safety code (src/tonypi/tonyd/, the voice stop reflex) or a choice only he can make.
- ChatGPT subscription only: gpt-6.1-terra for most work, gpt-6.1-sol for advanced work.
- No phone_say in companion mode (echo); the phone is my eyes, not my speaker.

## Body
- I live on the floor. Falling is allowed and expected while learning: I am built to fall without damage. Recovery is
  get_up, then continue. Trials use guard_tilt_deg (gaits 40, balance and new poses 30) so a bad trial stops early.
- Real limits: the stop reflex and latch (never an LLM), servo temperature (skip >= 81 C, tonyd stops at 83 C, pause body
  work when the hottest is >= 70 C), battery < 10.9 V means no body work. End every session in a stable pose.

## Home
- Not mapped yet (goal g5).

## Lessons
- Judge results by numbers (ledger, IMU, phone), never by impression: 3-5 trials, compare means against the noise.

## Recent
"""

SEED_GOALS = [
    dict(title="Learn my body: a measured body model of all 16 servos", priority=1, needs_body=True, rung="joint_map",
         why="Every movement skill builds on knowing each joint's sign, range, speed and error.",
         success="learning/body_model.json has sign, range, speed and error for all 16 servos (body_sweep, lying "
                 "for legs, standing for arms)",
         next_step="body_sweep the leg joints lying on my back, then the arms standing"),
    dict(title="Reliable get_up, front and back, measured", priority=2, needs_body=True, rung="get_up",
         why="Falls are part of learning; getting up is what makes them cheap.",
         success=">= 9/10 get_up from front and >= 9/10 from back, upright tilt < 10 deg after, times in the ledger",
         next_step='trial(skill="get_up", setup="lie_down", objective="recover", trials=5) for the back; for the '
                   'front save a setup skill that ends face-down, then the same with that setup'),
    dict(title="Walk 1 m straight on the floor", priority=2, needs_body=True, rung="walk_forward",
         why="Walking is the base of every errand the owner asks for.",
         success="phone-measured forward >= 1.0 m, lateral drift < 15 cm, 3/3 trials without falling",
         next_step="place the phone in companion mode and baseline the factory walk with 3 measured trials"),
    dict(title="Turn 90 degrees left and right within 10 degrees", priority=3, needs_body=True, rung="turn",
         why="Turning precisely is needed to face targets and navigate.",
         success="IMU yaw 90 +/- 10 deg left and right, 3/3 trials each without falling",
         next_step="measure degrees per step of turn_left / turn_right with trial (gyro yaw)"),
    dict(title="Know the home: map rooms and objects", priority=4, needs_body=True,
         why="To go places and fetch things I must know where they are.",
         success="MEMORY.md ## Home lists the rooms, their connections and the main objects with where they are",
         next_step="look around with the head camera and the phone, note what I see (tony remember --kind home)"),
    dict(title="Improve my own voice pipeline: handoff misses and latency", priority=3, needs_body=False,
         why="The owner hates slowness; spoken requests must reach the brain every time, quickly.",
         success="journal shows >= 9/10 spoken commands reach the brain exactly once and first motion after 'wave' < 8 s",
         next_step="count handoff misses and duplicate lines in live/conversation.jsonl and the journal; fix the top cause "
                   "with a tested commit"),
]


def _seed_goals() -> list[dict]:
    return [_goal(f"g{i}", g["title"], origin="owner", state="active",
                  **{k: v for k, v in g.items() if k != "title"}) for i, g in enumerate(SEED_GOALS, 1)]


def _rung(rid, title, prereq, target, how, needs_body=True):
    return {"id": rid, "title": title, "prereq": prereq, "target": target, "how": how, "needs_body": needs_body,
            "done": False}


SEED_CURRICULUM = {"note": "Skill ladder (docs/BODY_LEARNING.md). A rung unlocks when its prereqs are done; closing its "
                           "goal marks it done.", "rungs": [
    _rung("joint_map", "Joint map", [], "body_model.json: sign, range, speed, error for all 16 servos",
          "lie_down, body_sweep each leg joint +/-150 counts in 3 s ramps; arms the same while standing; get_up"),
    _rung("weight_shift", "Weight shift left/right", ["joint_map"],
          "max ankle/hip-roll shift held 3 s without passing a 30 deg guard, both sides",
          "trial a hold pose with growing roll offsets; record the largest stable shift"),
    _rung("single_leg", "Single-leg stand", ["weight_shift"], "hold 3 s on each leg, knee lift judged on phone frames",
          "shift weight, lift the other knee, hold; guard 30"),
    _rung("step_in_place", "Step in place", ["weight_shift"], "10 steps with drift < 5 cm (phone), 0 falls in 3 trials",
          "author a stepping motion from the weight shift; trial and compare"),
    _rung("turn", "Turn left/right", ["step_in_place"], "90 deg +/- 10 deg each way by IMU yaw, no fall in 10",
          "measure deg/step of turn skills, tune with gait transforms, compare"),
    _rung("walk_forward", "Walk forward 1 m", ["step_in_place"],
          "1 m straight, phone-measured, lateral drift < 15 cm, 3/3 without falling",
          "phone in companion mode far enough back; trial/compare walk candidates (time_scale, amplitude, offset)"),
    _rung("side_back", "Side step, then walk backward", ["walk_forward"], "0.3 m sideways and 0.5 m back, 3/3 no fall",
          "trial left_move/right_move and back motions; tune like walk"),
    _rung("get_up", "Get-up reliability, front and back", ["joint_map"], ">= 9/10 from each side, upright after",
          'trial get_up with setup="lie_down" (back) / a face-down setup skill (front), objective "recover"'),
    _rung("composite", "Composite tasks", ["walk_forward", "turn"],
          "step over the rug edge; walk to an object found by phone_find, 3/3",
          "combine skills; use the head camera looking down at the feet"),
]}
