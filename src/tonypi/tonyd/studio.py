"""The editable motion library: brains write their own moves, skills and calibration here.

Three layers, all plain JSON so any brain (or a person) can read, diff and edit them:
- motions: keyframes `{"frames": [{"ms": 400, "pos": {"1": 500, ...}}]}`. The owner-imported factory motions in `~/.local/share/tonypi/factory-motions`
  are the defaults/examples; a user motion with the same name overrides one (delete it to get the default back).
- skills: a named sequence of motions, `"*name"` repeats with `count` (e.g. walk = start, *step, end). User skills add
  new ones or override the default sequences in body.py (walk_forward, turn_left, ...).
- calibration: per-servo `trim` (counts added to every target, e.g. to straighten a leg) and soft `limits`
  `[lo, hi]` that every target is clamped to. Below that sit the servos' own EEPROM offset and angle limits
  (tonyd `servo_calibration` / `servo_calibrate`), only for fixing a bad calibration.

The user layer lives outside the repo (`TONYPI_USER_DIR`, default ~/.local/share/tonypi) so every harness, worktree
and session sees the same live robot. tonyd still guards whatever is played: stop, temperature, limits.
"""

from __future__ import annotations

import json
import os
import re
import threading
from pathlib import Path

from tonypi.motion.library import DATA_DIR, Frame, Motion

USER_DIR = Path(os.environ.get("TONYPI_USER_DIR", Path.home() / ".local/share/tonypi"))
NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,47}$")
SERVO_IDS = range(1, 17)
OPTIONAL_IDS = {17, 18}  # grippers on the Pro variant only; ignored here
MIN_FRAME_MS, MAX_FRAME_MS, MAX_FRAMES = 20, 10_000, 400


class StudioError(ValueError):
    pass


def _name(name: str, what: str) -> str:
    if not isinstance(name, str) or not NAME_RE.match(name):
        raise StudioError(f"bad {what} name {name!r}: lowercase letters, digits and _, starting with a letter")
    return name


def parse_frames(frames) -> list[Frame]:
    """Validates keyframes from a brain: ms 20..10000 per frame, servo ids 1..16, positions 0..1000."""
    if not isinstance(frames, list) or not 1 <= len(frames) <= MAX_FRAMES:
        raise StudioError(f"frames must be a list of 1..{MAX_FRAMES} keyframes")
    out = []
    for i, f in enumerate(frames):
        try:
            ms = int(f["ms"])
            pos = {int(k): int(v) for k, v in f["pos"].items()}
        except (KeyError, TypeError, ValueError, AttributeError) as e:
            raise StudioError(f"frame {i}: needs {{'ms': int, 'pos': {{servo_id: position}}}} ({e})") from e
        pos = {k: v for k, v in pos.items() if k not in OPTIONAL_IDS}  # factory files carry the absent grippers
        if not MIN_FRAME_MS <= ms <= MAX_FRAME_MS:
            raise StudioError(f"frame {i}: ms must be {MIN_FRAME_MS}..{MAX_FRAME_MS}")
        bad = {k: v for k, v in pos.items() if k not in SERVO_IDS or not 0 <= v <= 1000}
        if bad or not pos:
            raise StudioError(f"frame {i}: servo ids 1..16 with positions 0..1000 (bad: {bad or 'empty'})")
        out.append(Frame(ms, pos))
    return out


def frames_json(frames: list[Frame]) -> list[dict]:
    return [{"ms": f.ms, "pos": {str(k): v for k, v in sorted(f.pos.items())}} for f in frames]


class Studio:
    def __init__(self, user_dir: Path = USER_DIR, defaults: Path = DATA_DIR,
                 default_skills: dict[str, dict] | None = None) -> None:
        self.user_dir, self.defaults = Path(user_dir), Path(defaults)
        self.motions_dir = self.user_dir / "motions"
        self.default_skills = default_skills or {}
        self._lock = threading.Lock()

    # ---- motions ------------------------------------------------------------------------------------------------

    def _motion_path(self, name: str) -> Path | None:
        for d in (self.motions_dir, self.defaults):
            if (p := d / f"{name}.json").exists():
                return p
        return None

    def load_motion(self, name: str) -> Motion:
        p = self._motion_path(name)
        if p is None:
            raise StudioError(f"no motion {name!r}; import owner-supplied factory actions with tools/import_factory_motions.py (docs/FACTORY_MOTIONS.md)")
        raw = json.loads(p.read_text())
        return Motion(name, [Frame(f["ms"], {int(k): v for k, v in f["pos"].items()}) for f in raw["frames"]])

    def list_motions(self) -> list[dict]:
        user = {p.stem for p in self.motions_dir.glob("*.json")} if self.motions_dir.exists() else set()
        out = []
        for name in sorted(user | {p.stem for p in self.defaults.glob("*.json")}):
            m = self.load_motion(name)
            out.append({"name": name, "source": "user" if name in user else "default",
                        "frames": len(m.frames), "ms": m.duration_ms})
        return out

    def get_motion(self, name: str) -> dict:
        p = self._motion_path(_name(name, "motion"))
        if p is None:
            raise StudioError(f"no motion {name!r}; import owner-supplied factory actions with tools/import_factory_motions.py (docs/FACTORY_MOTIONS.md)")
        raw = json.loads(p.read_text())
        return {"name": name, "source": "user" if p.parent == self.motions_dir else "default",
                "description": raw.get("description", ""), "frames": raw["frames"]}

    def save_motion(self, name: str, frames, description: str = "") -> dict:
        _name(name, "motion")
        parsed = parse_frames(frames)
        data = {"name": name, "description": str(description)[:500], "frames": frames_json(parsed)}
        self._write(self.motions_dir / f"{name}.json", data)
        return {"saved": name, "frames": len(parsed), "ms": sum(f.ms for f in parsed),
                "overrides_default": (self.defaults / f"{name}.json").exists()}

    def delete_motion(self, name: str) -> dict:
        p = self.motions_dir / f"{_name(name, 'motion')}.json"
        if not p.exists():
            raise StudioError(f"no user motion {name!r} (defaults can't be deleted, only overridden)")
        p.unlink()
        return {"deleted": name, "default_restored": (self.defaults / f"{name}.json").exists()}

    # ---- skills -------------------------------------------------------------------------------------------------

    def _user_skills(self) -> dict[str, dict]:
        p = self.user_dir / "skills.json"
        return json.loads(p.read_text()) if p.exists() else {}

    def skills(self) -> dict[str, dict]:
        """name -> {motions, description, source}: the defaults overlaid with the user's."""
        out = {k: {**v, "source": "default"} for k, v in self.default_skills.items()}
        for k, v in self._user_skills().items():
            out[k] = {**v, "source": "user"}
        return out

    def save_skill(self, name: str, motions: list[str], description: str = "") -> dict:
        _name(name, "skill")
        if not isinstance(motions, list) or not motions:
            raise StudioError("motions: a list of motion names; '*name' repeats with count")
        for m in motions:
            if self._motion_path(_name(str(m).lstrip("*"), "motion")) is None:
                raise StudioError(f"no motion {m!r} (save it first)")
        with self._lock:
            skills = self._user_skills()
            skills[name] = {"motions": [str(m) for m in motions], "description": str(description)[:300]}
            self._write(self.user_dir / "skills.json", skills)
        return {"saved": name, "overrides_default": name in self.default_skills}

    def delete_skill(self, name: str) -> dict:
        with self._lock:
            skills = self._user_skills()
            if skills.pop(name, None) is None:
                raise StudioError(f"no user skill {name!r}")
            self._write(self.user_dir / "skills.json", skills)
        return {"deleted": name, "default_restored": name in self.default_skills}

    # ---- calibration --------------------------------------------------------------------------------------------

    def calibration(self) -> dict:
        p = self.user_dir / "calibration.json"
        raw = json.loads(p.read_text()) if p.exists() else {}
        return {"trim": {int(k): int(v) for k, v in raw.get("trim", {}).items()},
                "limits": {int(k): [int(v[0]), int(v[1])] for k, v in raw.get("limits", {}).items()}}

    def set_calibration(self, trim: dict | None = None, limits: dict | None = None) -> dict:
        """Merges into the stored calibration; a trim of 0 or limits of null removes that servo's entry."""
        with self._lock:
            cal = self.calibration()
            for k, v in (trim or {}).items():
                sid, v = int(k), int(v)
                if sid not in SERVO_IDS or not -200 <= v <= 200:
                    raise StudioError(f"trim: servo ids 1..16, -200..200 counts (got {k}: {v})")
                cal["trim"].pop(sid, None) if v == 0 else cal["trim"].__setitem__(sid, v)
            for k, v in (limits or {}).items():
                sid = int(k)
                if sid not in SERVO_IDS:
                    raise StudioError(f"limits: servo ids 1..16 (got {k})")
                if v is None:
                    cal["limits"].pop(sid, None)
                    continue
                lo, hi = int(v[0]), int(v[1])
                if not 0 <= lo < hi <= 1000:
                    raise StudioError(f"limits for {sid}: need 0 <= lo < hi <= 1000")
                cal["limits"][sid] = [lo, hi]
            self._write(self.user_dir / "calibration.json",
                        {"trim": {str(k): v for k, v in sorted(cal["trim"].items())},
                         "limits": {str(k): v for k, v in sorted(cal["limits"].items())}})
        return cal

    def to_servo(self, cal: dict, pos: dict[int, int]) -> dict[int, int]:
        """Motion space -> servo targets: add the trim, clamp to the soft limits and 0..1000."""
        out = {}
        for sid, p in pos.items():
            p += cal["trim"].get(sid, 0)
            lo, hi = cal["limits"].get(sid, (0, 1000))
            out[sid] = max(lo, min(hi, max(0, min(1000, p))))
        return out

    # ---- io -----------------------------------------------------------------------------------------------------

    @staticmethod
    def _write(path: Path, data) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1) + "\n")
        tmp.replace(path)
