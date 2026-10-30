"""Runs measured trials on the robot and keeps the records: the ledger, the skill library and the body model.

One trial: preflight (tonyd idle, no stop latch, battery >= 10.9 V, hottest servo < 70 C; lying -> get_up first),
settle to a stand, fresh state, phone recording (companion mode only), the move with a tilt guard and a label, a 1.5 s
tail, fresh state, stop the recording, score (metrics.py), one ledger line. A fall is data: get_up (at most twice,
then the batch stops and says so) and on to the next trial. The whole batch holds /run/lock/tonypi-robot.lock
(non-blocking: held by someone else -> refused) and every move goes through tonyd, so stop and the temperature guard
work as always. A cancelled job (the owner said stop, or another brain moved) ends the batch WITHOUT the closing
settle / get_up (that would preempt the other brain's move); so does the autonomy preempt mark (tonypi.brain.
preempt_file: the voice service ended the session this batch belongs to), checked before every move.
Trials start standing (get_up if lying, settle) unless the spec says start "as_is" or names a `setup` skill (e.g.
lie_down, then measure get_up with objective "recover").

Records in LEARN_DIR (tonypi.brain): ledger.jsonl (one line per trial or swept joint), library.json (per skill key:
candidates with their scores, incumbent, best, falls, evidence), body_model.json (per joint: sign, range, error).
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import logging
import os
import time
from pathlib import Path

from tonypi.learn import gait, metrics
from tonypi.learn.phone import Phone, PhoneError
from tonypi.tonyd.client import TonydClient, TonydError

log = logging.getLogger(__name__)

ROBOT_LOCK = os.environ.get("TONYPI_ROBOT_LOCK", "/run/lock/tonypi-robot.lock")
MIN_BATTERY_MV = 10_900
MAX_HOT_C = 70  # pause body work at this servo temperature (tonyd itself skips at 81, stops at 83)
TAIL_S = 1.5  # after the move: let Tony settle (or finish falling) before reading the state
GET_UP_TRIES = 2
MAX_TRIALS = 30
PROMOTE_MIN_N = 5
SWEEP_COUNTS, SWEEP_MS, RETURN_MS = 150, 3000, 1500
LEGS, ARMS = [1, 2, 3, 4, 5, 9, 10, 11, 12, 13], [6, 7, 8, 14, 15, 16]
KEEP_SCORES, KEEP_EVIDENCE = 50, 20


class LabError(RuntimeError):
    """A trial or batch can't start (or must stop): the message says why, for the brain to act on."""


class Interrupted(LabError):
    """Someone else has the body now (a cancelled job, or the autonomy preempt mark): stop without cleanup moves."""


def learn_dir() -> Path:
    import tonypi.brain

    return Path(tonypi.brain.LEARN_DIR)


def _hot(st: dict) -> tuple | None:
    h = st.get("hottest")
    return tuple(h) if h and h[2] is not None and h[2] >= MAX_HOT_C else None


class Lab:
    def __init__(self, tonyd: TonydClient | None = None, phone: Phone | None = None, root: Path | None = None,
                 lock_path: str | None = None, use_phone: bool = True, sleep=time.sleep,
                 preempt_path: str | Path | None = None) -> None:
        self.tonyd = tonyd or TonydClient()
        self.phone = phone if phone is not None else (Phone() if use_phone else None)
        self.root = Path(root) if root is not None else learn_dir()
        self.lock_path = lock_path or ROBOT_LOCK
        self.sleep = sleep
        self.preempt_path = Path(preempt_path) if preempt_path is not None else None
        self.session = time.strftime("%m%d-%H%M%S")
        self._n = 0
        self._t0: float | None = None  # wall-clock start of the running batch (for the preempt mark)

    # ---- records -----------------------------------------------------------------------------------------------

    @property
    def ledger_path(self) -> Path:
        return self.root / "ledger.jsonl"

    def _append(self, row: dict) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        with open(self.ledger_path, "a") as f:
            f.write(json.dumps(row, separators=(",", ":")) + "\n")

    def _load(self, name: str) -> dict:
        try:
            return json.loads((self.root / name).read_text())
        except (OSError, ValueError):
            return {}

    def _save(self, name: str, obj: dict) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self.root / f".{name}.tmp"
        tmp.write_text(json.dumps(obj, indent=1) + "\n")
        tmp.replace(self.root / name)

    def ledger(self, skill: str | None = None, label: str | None = None, limit: int = 20) -> list[dict]:
        """The newest ledger rows (oldest first), filtered by skill key / candidate / what, and label."""
        rows = []
        try:
            lines = self.ledger_path.read_text().splitlines()
        except OSError:
            return []
        for line in reversed(lines):
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if skill and skill not in (r.get("key"), r.get("cand"), r.get("what"), r.get("skill")):
                continue
            if label and r.get("label") != label:
                continue
            rows.append(r)
            if len(rows) >= max(1, int(limit)):
                break
        return rows[::-1]

    def library(self) -> dict:
        return self._load("library.json")

    def body_model(self) -> dict:
        return self._load("body_model.json")

    def _library_add(self, key: str, row: dict, surface: str | None) -> None:
        lib = self._load("library.json")
        e = lib.setdefault(key, {"objective": row["objective"], "incumbent": None, "best": None, "candidates": {},
                                 "evidence": []})
        if surface:
            e["surface"] = surface
        c = e["candidates"].setdefault(row["cand"], {"what": row["what"], "params": row.get("params"), "scores": [],
                                                      "falls": 0})
        c["scores"] = (c["scores"] + [row["score"]])[-KEEP_SCORES:]
        c["falls"] += bool(row["metrics"].get("fell"))
        c.update(metrics.stats(c["scores"]), last_t=row["t"])
        if e["incumbent"] is None and row.get("skill") == key and not row.get("params"):
            e["incumbent"] = row["cand"]  # the skill as tonyd runs it now
        ok = {h: v for h, v in e["candidates"].items() if v["n"] >= 3 and v["falls"] == 0}
        e["best"] = max(ok, key=lambda h: ok[h]["mean"]) if ok else e["best"]
        e["evidence"] = (e["evidence"] + [row["exp"]])[-KEEP_EVIDENCE:]
        e["updated"] = row["t"]
        lib[key] = e
        self._save("library.json", lib)

    # ---- robot -------------------------------------------------------------------------------------------------

    @contextlib.contextmanager
    def locked(self):
        """The robot lock for the whole batch. TONYPI_ROBOT_LOCK_HELD=1: the caller's slot already holds it."""
        if os.environ.get("TONYPI_ROBOT_LOCK_HELD") == "1":
            yield
            return
        fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o666)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise LabError(f"the robot lock {self.lock_path} is held by another job (a test or another learning "
                               "batch): try again when it is free") from None
            with contextlib.suppress(OSError):  # who holds it, for the autonomy scheduler's probe
                os.ftruncate(fd, 0)
                os.pwrite(fd, f"lab {os.getpid()}\n".encode(), 0)
            try:
                yield
            finally:
                with contextlib.suppress(OSError):
                    os.ftruncate(fd, 0)
        finally:
            os.close(fd)

    def _state(self) -> dict:
        return self.tonyd.state(fresh=True)

    def _preempted(self) -> bool:
        """The voice service ended the autonomy session after this batch started (tonypi.brain.preempt_file)."""
        if self._t0 is None:
            return False
        if self.preempt_path is None:
            import tonypi.brain

            path = tonypi.brain.preempt_file()
        else:
            path = self.preempt_path
        try:
            return float(path.read_text().strip() or 0) >= self._t0
        except (OSError, ValueError):
            return False

    def _check(self) -> None:
        """Before every move: stop (no cleanup moves) once the session this batch belongs to was ended."""
        if self._preempted():
            raise Interrupted("preempted: the autonomy session was ended (the owner spoke, a pause, the time cap or "
                              "a stop), so the batch stopped without moving again; the body is the owner's now")

    def _get_up(self) -> dict:
        """get_up at most GET_UP_TRIES times; raises LabError when Tony is still lying after that."""
        for _ in range(GET_UP_TRIES):
            self._check()
            job = self.tonyd.run("get_up", timeout=90)
            if job["state"] == "cancelled":
                raise Interrupted("get_up was cancelled (stop, or another brain moved)")
            self.sleep(TAIL_S)
            st = self._state()
            if not st.get("lying"):
                return st
        raise LabError(f"still lying after {GET_UP_TRIES} get_up attempts: stopped (check with the camera/phone "
                       "what is in the way before trying again)")

    def preflight(self, get_up: bool = True) -> dict:
        """Refuses (LabError) unless the body is free and fit; gets Tony up first if he is lying (get_up=False: the
        trial starts as he is)."""
        self._check()
        st = self._state()
        if st.get("busy"):
            raise LabError("the body is busy with another job")
        if st.get("stop_latched"):
            raise LabError("stop latch is on: the owner said stop; no body work until he asks for a move")
        mv = st.get("battery_mv")
        if mv is not None and mv < MIN_BATTERY_MV:
            raise LabError(f"battery {mv / 1000:.2f} V is below {MIN_BATTERY_MV / 1000} V: no body work until charged")
        if hot := _hot(st):
            raise LabError(f"servo {hot[0]} ({hot[1]}) is at {hot[2]} C (>= {MAX_HOT_C}): let it cool, do "
                           "non-body work")
        if get_up and st.get("lying"):
            st = self._get_up()
        return st

    # ---- trials ------------------------------------------------------------------------------------------------

    def prepare(self, spec: dict | str) -> dict:
        """Resolve a spec: skill name | frames | base motion + gait params -> what to run and its candidate hash.
        spec keys: skill, frames, base, gait {time_scale, amplitude, offset, mirror, repeat}, count, objective,
        guard_tilt_deg, label, hold_s, trace, settle (default true), surface, start ("stand" (default): get_up if lying,
        then settle | "as_is": neither), setup (a skill run unmeasured before each trial, e.g. lie_down; implies
        start "as_is"; the measured move then starts from where the setup left Tony)."""
        spec = {"skill": spec} if isinstance(spec, str) else dict(spec)
        objective = spec.get("objective") or "generic"
        if objective not in metrics.OBJECTIVES:
            raise LabError(f"objective is one of {', '.join(metrics.OBJECTIVES)}")
        count = max(1, min(5, int(spec.get("count") or 1)))
        guard = spec.get("guard_tilt_deg")
        if guard is None:
            guard = metrics.GUARD_DEG[objective]
        setup = str(spec["setup"]) if spec.get("setup") else None
        start = spec.get("start") or ("as_is" if setup else "stand")
        if start not in ("stand", "as_is"):
            raise LabError('start is "stand" (get up if lying, settle) or "as_is"')
        if setup and setup not in self.tonyd.call("skills")["details"]:
            raise LabError(f"unknown setup skill {setup!r}")
        p = {"objective": objective, "count": count, "label": spec.get("label"), "hold_s": spec.get("hold_s") or 0,
             "trace": bool(spec.get("trace")), "settle": spec.get("settle", True) and start == "stand",
             "surface": spec.get("surface"), "start": start, "setup": setup,
             "guard": float(guard) if guard is not None else None, "params": spec.get("gait") or None}
        sources = [k for k in ("skill", "frames", "base") if spec.get(k)]
        if len(sources) != 1:
            raise LabError("give exactly one of skill, frames or base (a motion name, with gait params)")
        if spec.get("skill"):
            skill = str(spec["skill"])
            details = self.tonyd.call("skills")["details"]
            if skill not in details:
                raise LabError(f"unknown skill {skill!r}")
            canon = json.dumps([skill, details[skill].get("motions")], separators=(",", ":"))
            p.update(skill=skill, what=skill, cand=hashlib.sha1(canon.encode()).hexdigest()[:10], params=None)
        else:
            if spec.get("base"):
                base = self.tonyd.call("motion_get", name=str(spec["base"]))
                m = gait.apply(base, spec.get("gait"))
                what = f"{spec['base']}" + (f" {json.dumps(spec['gait'], separators=(',', ':'))}"
                                            if spec.get("gait") else "")
            else:
                m, what = gait.apply({"frames": spec["frames"]}, None), f"{len(spec['frames'])} keyframes"
            if not m["frames"]:
                raise LabError("no keyframes to play")
            one = m["frames"]
            p.update(skill=None, frames=gait.repeat(m, count)["frames"], what=what, cand=gait.digest(one),
                     ms=sum(f["ms"] for f in one) * count)
        if setup:
            p["what"] += f" (after {setup})"
        base_key = p["skill"] or spec.get("base") or "frames"
        p["key"] = spec.get("key") or spec.get("label") or (f"{base_key}@{setup}" if setup else base_key)
        return p

    def _phone_ready(self) -> bool:
        if self.phone is None:
            return False
        return self.phone.mode() == "companion"

    def _start_tracking(self) -> str | None:
        try:
            self.phone.track()
            return None
        except PhoneError as e:
            return str(e)

    def _one(self, p: dict, phone_on: bool) -> dict:
        self._n += 1
        exp = f"{self.session}-{self._n}"
        setup = None
        if p["settle"]:
            self._check()
            job = self.tonyd.run("settle", timeout=30)
            if job["state"] == "cancelled":
                raise Interrupted("settle was cancelled (stop, or another brain moved)")
        if p.get("setup"):
            self._check()
            job = self.tonyd.run(p["setup"], timeout=90, trace=p["trace"] or None)
            setup = {"skill": p["setup"], "job": job}
            if job["state"] == "cancelled":
                raise Interrupted(f"setup {p['setup']} was cancelled (stop, or another brain moved)")
            if job["state"] != "done":
                raise LabError(f"setup {p['setup']} {job['state']}: trial not run")
            self.sleep(TAIL_S)
        self._check()
        before = self._state()
        note, rec = None, False
        if phone_on:
            try:
                self.phone.record_start(f"exp-{exp}", max_s=(p.get("ms") or 30_000 * p["count"]) / 1000 + 10)
                rec = True
            except PhoneError as e:
                note = str(e)
        kw = {"guard_tilt_deg": p["guard"], "label": exp, "trace": p["trace"] or None}
        try:
            if p["skill"]:
                job = self.tonyd.run(p["skill"], p["count"], timeout=120, hold_s=p["hold_s"], **kw)
            else:
                job = self.tonyd.play(p["frames"], hold_s=p["hold_s"], timeout=120, **kw)
        finally:
            self.sleep(TAIL_S)
            after = self._state()
            episode = None
            if rec:
                try:
                    meta = self.phone.record_stop()
                    episode = self.phone.episode(meta.get("id") or Path(meta.get("path", "")).name)
                    if episode.get("error") and meta.get("target_moved_m") is not None:  # folder not on this Pi
                        episode = {k: meta.get(k) for k in ("id", "frames", "frames_tracked", "target_moved_m")}
                except PhoneError as e:
                    note = str(e)
        m = metrics.score(p["objective"], job, before, after, episode, p["count"])
        verdict = "fell" if m["fell"] else {"done": "ok"}.get(job["state"], job["state"])
        row = {"exp": exp, "t": round(time.time(), 2), "kind": "trial", "key": p["key"], "label": p["label"],
               "skill": p["skill"], "what": p["what"], "cand": p["cand"], "params": p["params"], "count": p["count"],
               "objective": p["objective"], "guard_tilt_deg": p["guard"], "metrics": m, "score": m["score"],
               "episode": (episode or {}).get("id"), "job": job.get("id"), "trace": job.get("trace"),
               "before": {k: before.get(k) for k in ("lying", "tilt_deg")}, "setup": setup,
               "verdict": verdict, "said": job.get("said") or None, "phone": note,
               "battery_mv": after.get("battery_mv"), "hottest": after.get("hottest")}
        row["note"] = (f"{p['what']} x{p['count']} ({p['objective']}): score {m['score']}, {verdict}, "
                       f"tilt {m['max_tilt_deg']}, yaw {m['yaw_deg']}, disp {m['disp_m']}")
        self._append(row)
        self._library_add(p["key"], row, p["surface"])
        return row

    def _run(self, plan: list[dict], budget_s: float | None) -> tuple[list[dict], str | None]:
        """Runs prepared specs in order under the lock. Returns (rows, why it stopped early or None)."""
        rows, stopped, t0, interrupted = [], None, time.monotonic(), False
        self._t0 = time.time()
        with self.locked():
            phone_on = self._phone_ready()
            if phone_on and self._start_tracking():
                log.info("phone tracking failed; recording without it")
            for p in plan:
                if budget_s is not None and rows:
                    per = (time.monotonic() - t0) / len(rows)
                    if time.monotonic() - t0 + per > budget_s:
                        stopped = f"time budget {budget_s:.0f} s: ran {len(rows)} of {len(plan)}"
                        break
                try:
                    self.preflight(get_up=p.get("start", "stand") == "stand")
                    row = self._one(p, phone_on)
                except (LabError, TonydError) as e:
                    stopped, interrupted = str(e), isinstance(e, Interrupted)
                    break
                rows.append(row)
                if row["verdict"] == "cancelled":
                    stopped = "the move was cancelled (stop, or another brain moved): batch ended, no closing settle"
                    interrupted = True
                    break
                if row["metrics"]["fell"]:
                    try:
                        self._get_up()
                    except (LabError, TonydError) as e:
                        stopped, interrupted = str(e), isinstance(e, Interrupted)
                        break
            if rows and not interrupted:
                self._end_stable()
        return rows, stopped

    def _end_stable(self) -> None:
        """End in a stable stand (going limp mid-step can tip Tony over); not while latched, lying or preempted."""
        if self._preempted():
            return
        with contextlib.suppress(TonydError):
            st = self.tonyd.state()
            if not st.get("lying") and not st.get("stop_latched"):
                self.tonyd.run("settle", timeout=30)

    @staticmethod
    def _compact(r: dict) -> dict:
        m = r["metrics"]
        out = {"exp": r["exp"], "verdict": r["verdict"], "score": r["score"], "max_tilt_deg": m["max_tilt_deg"],
               "yaw_deg": m["yaw_deg"], "disp_m": m["disp_m"], "s": m["seconds"], "job": r["job"]}
        if r.get("said"):
            out["said"] = r["said"]
        if r.get("phone"):
            out["phone"] = r["phone"]
        return out

    @staticmethod
    def _summary(rows: list[dict]) -> dict:
        s = metrics.stats([r["score"] for r in rows])
        tilts = [r["metrics"]["max_tilt_deg"] for r in rows if r["metrics"]["max_tilt_deg"] is not None]
        yaws = [r["metrics"]["yaw_deg"] for r in rows if r["metrics"]["yaw_deg"] is not None]
        return {**s, "falls": sum(r["metrics"]["fell"] for r in rows),
                "mean_max_tilt_deg": round(sum(tilts) / len(tilts), 1) if tilts else None,
                "mean_yaw_deg": round(sum(yaws) / len(yaws), 1) if yaws else None}

    def batch(self, spec: dict | str, trials: int = 3, budget_s: float | None = None) -> dict:
        """`trials` measured runs of one candidate (1..30). Returns per-trial metrics and a summary."""
        p = self.prepare(spec)
        n = max(1, min(MAX_TRIALS, int(trials)))
        rows, stopped = self._run([p] * n, budget_s)
        return {"what": p["what"], "key": p["key"], "cand": p["cand"], "objective": p["objective"],
                "guard_tilt_deg": p["guard"], "trials": [self._compact(r) for r in rows],
                "summary": self._summary(rows), "stopped": stopped}

    def trial(self, spec: dict | str) -> dict:
        return self.batch(spec, 1)

    def compare(self, base: dict | str, candidate: dict | str, trials: int = 5, objective: str | None = None,
                budget_s: float | None = None) -> dict:
        """Interleaved A/B trials (base, candidate, base, ...) so heat and battery drift hit both alike. Verdict
        "promote" = candidate mean beats base by more than 1 pooled std over >= 5 trials each with 0 falls."""
        sb, sc = [{"skill": s} if isinstance(s, str) else dict(s) for s in (base, candidate)]
        for s in (sb, sc):
            if objective:
                s["objective"] = objective
        pb = self.prepare(sb)
        sc.setdefault("objective", pb["objective"])
        key = sc.get("key") or sb.get("label") or pb["key"]
        pb["key"] = key
        pc = self.prepare({**sc, "key": key})
        n = max(1, min(MAX_TRIALS // 2, int(trials)))
        rows, stopped = self._run([q for _ in range(n) for q in (pb, pc)], budget_s)
        rb, rc = rows[0::2], rows[1::2]  # the plan alternates base, candidate
        b, c = self._summary(rb), self._summary(rc)
        verdict = self.verdict(b, c)
        if verdict == "promote":
            lib = self._load("library.json")
            if key in lib:
                lib[key]["best"], lib[key]["promote_suggested"] = pc["cand"], {"over": pb["cand"], "t": time.time()}
                self._save("library.json", lib)
        return {"key": key, "objective": pb["objective"], "base": {"what": pb["what"], "cand": pb["cand"], **b},
                "candidate": {"what": pc["what"], "cand": pc["cand"], **c}, "verdict": verdict,
                "trials": [self._compact(r) | {"side": ("base", "candidate")[i % 2]} for i, r in enumerate(rows)],
                "stopped": stopped}

    @staticmethod
    def verdict(b: dict, c: dict) -> str:
        if c["falls"] > b["falls"] and c["falls"] > 0:
            return "reject: the candidate fell more"
        if not b["n"] or not c["n"]:
            return "no data"
        pooled = (((b["std"] or 0) ** 2 + (c["std"] or 0) ** 2) / 2) ** 0.5
        diff = c["mean"] - b["mean"]
        if min(b["n"], c["n"]) < PROMOTE_MIN_N:
            return f"more trials needed (>= {PROMOTE_MIN_N} each; difference so far {diff:+.3f}, std {pooled:.3f})"
        if diff > pooled and c["falls"] == 0:
            return "promote"
        if -diff > pooled:
            return "worse"
        return "no clear difference"

    # ---- joint map ---------------------------------------------------------------------------------------------

    def sweep(self, joints: list[int] | None = None, lying: bool = True, budget_s: float | None = None) -> dict:
        """Joint map: lie down (legs unloaded) or stand (arms), then each joint +-150 counts in 3 s ramps from where
        it is, reading where it got to; records sign, reached range, error and IMU response in body_model.json,
        then gets up (or settles)."""
        joints = [int(j) for j in (joints or (LEGS if lying else ARMS)) if 1 <= int(j) <= 16]
        names, out, stopped, t0, started = gait.joint_names(), {}, None, time.monotonic(), False
        interrupted = False
        self._t0 = time.time()
        with self.locked():
            try:
                st = self.preflight() if not lying else self._fit_lying()
                started = True
                if lying and not st.get("lying"):
                    job = self.tonyd.run("lie_down", timeout=60)
                    if job["state"] == "cancelled":
                        raise Interrupted("lie_down was cancelled (stop, or another brain moved)")
                    self.sleep(TAIL_S)
                    if job["state"] != "done" or not self._state().get("lying"):
                        raise LabError(f"lie_down did not end lying ({job['state']})")
                elif not lying:
                    self._check()
                    if self.tonyd.run("stand", timeout=30)["state"] == "cancelled":
                        raise Interrupted("stand was cancelled (stop, or another brain moved)")
                base = {int(k): v for k, v in self.tonyd.joints()["pos"].items()}
                for j in joints:
                    spent = time.monotonic() - t0
                    if budget_s is not None and out and spent + spent / len(out) > budget_s:
                        stopped = f"time budget: swept {list(out)}, not {[x for x in joints if x not in out]}"
                        break
                    if hot := _hot(self._state()):
                        stopped = f"servo {hot[0]} at {hot[2]} C: paused to cool"
                        break
                    out[j] = self._sweep_joint(j, base.get(j), lying, names.get(j, str(j)))
            except (LabError, TonydError) as e:
                stopped, interrupted = str(e), isinstance(e, Interrupted)
            finally:
                try:  # not after a cancel / preempt: the body is someone else's now
                    if started and not interrupted and self._state().get("lying"):
                        self._get_up()
                    elif started and not interrupted:
                        self._end_stable()
                except (LabError, TonydError) as e:
                    stopped = f"{stopped + '; ' if stopped else ''}{e}"
        model = self._load("body_model.json")
        model.setdefault("joints", {})
        for j, r in out.items():
            prev = model["joints"].get(str(j), {})
            model["joints"][str(j)] = {**r, "n": prev.get("n", 0) + 1, "t": round(time.time(), 2)}
        if out:
            model["updated"] = round(time.time(), 2)
            self._save("body_model.json", model)
        return {"joints": out, "stopped": stopped, "body_model": str(self.root / "body_model.json")}

    def _fit_lying(self) -> dict:
        """Preflight without the get_up (the sweep wants him lying)."""
        self._check()
        st = self._state()
        if st.get("busy") or st.get("stop_latched"):
            raise LabError("the body is busy or the stop latch is on")
        mv = st.get("battery_mv")
        if mv is not None and mv < MIN_BATTERY_MV:
            raise LabError(f"battery {mv / 1000:.2f} V is below {MIN_BATTERY_MV / 1000} V")
        if hot := _hot(st):
            raise LabError(f"servo {hot[0]} at {hot[2]} C: let it cool")
        return st

    def _sweep_joint(self, j: int, b: int | None, lying: bool, name: str) -> dict:
        if b is None:
            raise LabError(f"servo {j} did not answer a position read")
        guard = None if lying else float(metrics.GUARD_DEG["balance"])
        res = {"name": name, "pose": "lying" if lying else "standing", "base": b}
        for sign, tag in ((1, "plus"), (-1, "minus")):
            self._check()
            target = max(0, min(1000, b + sign * SWEEP_COUNTS))
            job = self.tonyd.play([{"ms": SWEEP_MS, "pos": {str(j): target}}], hold_s=1, guard_tilt_deg=guard,
                                  label=f"sweep-{j}-{tag}", timeout=30)
            reached = self.tonyd.joints()["pos"].get(str(j))
            back = self.tonyd.play([{"ms": RETURN_MS, "pos": {str(j): b}}], hold_s=1, label=f"sweep-{j}-back",
                                   timeout=30)
            if "cancelled" in (job["state"], back["state"]):
                raise Interrupted("sweep cancelled (stop, or another brain moved)")
            res[tag] = {"target": target, "reached": reached, "moved": None if reached is None else reached - b,
                        "error": None if reached is None else target - reached, "rot_deg": job.get("rot_deg"),
                        "max_tilt_deg": job.get("max_tilt_deg"), "state": job["state"]}
        p, m = res["plus"]["moved"], res["minus"]["moved"]
        if p is not None and m is not None:
            res["sign"] = 1 if p > 10 and m < -10 else -1 if p < -10 and m > 10 else 0  # 0: blocked / didn't follow
            res["range_seen"] = [b + m, b + p]
            res["speed_cps"] = round(max(abs(p), abs(m)) / (SWEEP_MS / 1000), 1)
            res["max_error"] = max(abs(res["plus"]["error"]), abs(res["minus"]["error"]))
        row = {"exp": f"{self.session}-sweep-{j}", "t": round(time.time(), 2), "kind": "sweep", "key": f"joint{j}",
               "what": f"sweep {name} ({res['pose']})", "metrics": {k: res.get(k) for k in
                                                                   ("sign", "range_seen", "speed_cps", "max_error")},
               "note": f"servo {j} {name} {res['pose']}: sign {res.get('sign')}, range {res.get('range_seen')}, "
                       f"max error {res.get('max_error')}"}
        self._append(row)
        return res
