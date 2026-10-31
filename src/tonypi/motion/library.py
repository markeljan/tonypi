"""Keyframe motion library (converted from Hiwonder .d6a files) and a batched player."""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path

REPO_DATA_DIR = Path(__file__).resolve().parents[3] / "data"
DATA_DIR = Path(os.environ.get("TONYPI_FACTORY_MOTIONS_DIR", Path.home() / ".local/share/tonypi/factory-motions"))


def read_factory(name: str, directory: Path = DATA_DIR) -> dict:
    try:
        return json.loads((directory / f"{name}.json").read_text())
    except FileNotFoundError as exc:
        raise ValueError(f"factory motion {name!r} missing in {directory}; "
                         "import your own actions with tools/import_factory_motions.py "
                         "(see docs/FACTORY_MOTIONS.md)") from exc


@dataclass
class Frame:
    ms: int
    pos: dict[int, int]


@dataclass
class Motion:
    name: str
    frames: list[Frame]

    @property
    def duration_ms(self) -> int:
        return sum(f.ms for f in self.frames)


def load(name: str, directory: Path = DATA_DIR) -> Motion:
    raw = read_factory(name, directory)
    return Motion(raw["name"], [Frame(f["ms"], {int(k): v for k, v in f["pos"].items()}) for f in raw["frames"]])


def available(directory: Path = DATA_DIR) -> list[str]:
    return sorted(p.stem for p in directory.glob("*.json"))


class Player:
    """Plays motions as one batched move per frame on a monotonic schedule. Interruptible."""

    def __init__(self, board, servo_ids=range(1, 17)) -> None:
        self.board = board
        self.servo_ids = set(servo_ids)
        self._stop = threading.Event()
        self.playing: str | None = None
        self.transform = None  # optional fn(targets) -> targets, e.g. calibration trim + soft limits

    def stop(self) -> None:
        self._stop.set()

    def play(self, motion: Motion, lock: dict[int, int] | None = None, speed: float = 1.0) -> bool:
        """Blocking. Returns False if interrupted. `lock` overrides chosen servos (e.g. hold arms)."""
        self._stop.clear()
        self.playing = motion.name
        deadline = time.monotonic()
        try:
            for frame in motion.frames:
                if self._stop.is_set():
                    return False
                ms = max(20, int(frame.ms / speed))
                targets = {i: p for i, p in frame.pos.items() if i in self.servo_ids}
                if lock:
                    targets.update(lock)
                if self.transform:
                    targets = self.transform(targets)
                self.board.move(ms, targets)
                deadline += ms / 1000
                while (remaining := deadline - time.monotonic()) > 0:
                    if self._stop.wait(min(remaining, 0.02)):
                        return False
            return True
        finally:
            self.playing = None
