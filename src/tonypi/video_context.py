"""Bounded, retrospective head-camera context; only uses tonyd's public snapshot API."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
import math
from pathlib import Path
import threading
import time
from typing import Callable

import numpy as np


@dataclass(frozen=True)
class Frame:
    monotonic_s: float
    epoch_s: float
    jpeg: bytes
    head_deg: dict


class FrameHistory:
    fps = 2
    retention_s = 60
    max_frames = 120
    max_bytes = 8 * 1024 * 1024
    tile_width, tile_height = 320, 240

    def __init__(self, snapshot: Callable[[], dict]) -> None:
        self.snapshot = snapshot
        self._frames: deque[Frame] = deque()
        self._bytes = 0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.error: str | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="video-context", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=4)

    def _prune(self, now: float) -> None:
        while self._frames and (now - self._frames[0].monotonic_s > self.retention_s
                                or len(self._frames) > self.max_frames or self._bytes > self.max_bytes):
            self._bytes -= len(self._frames.popleft().jpeg)

    def capture(self) -> bool:
        import cv2

        shot = self.snapshot()
        received, epoch = time.monotonic(), time.time()
        age = float(shot["frame_age_s"])
        # Reject stalled cameras instead of filling the history with old duplicates.
        if not math.isfinite(age) or age < 0 or age > 0.75:
            raise ValueError("camera frame is stale")
        captured = received - age
        with self._lock:
            if self._frames and captured - self._frames[-1].monotonic_s < 0.5 / self.fps:
                return False
        # tonyd owns and rotates these files; never delete them or open the camera device here.
        with Path(shot["path"]).open("rb") as source:
            data = source.read(2 * 1024 * 1024 + 1)
        if len(data) > 2 * 1024 * 1024:
            raise ValueError("camera JPEG exceeds input limit")
        image = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
        if image is None:
            raise ValueError("invalid camera JPEG")
        image = cv2.resize(image, (self.tile_width, self.tile_height), interpolation=cv2.INTER_AREA)
        ok, jpeg = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 70])
        if not ok:
            raise ValueError("camera JPEG encoding failed")
        frame = Frame(captured, epoch - age, jpeg.tobytes(), dict(shot.get("head_deg", {})))
        with self._lock:
            self._frames.append(frame)
            self._bytes += len(frame.jpeg)
            self._prune(received)
            self.error = None
        return True

    def _loop(self) -> None:
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                self.capture()
            except Exception as exc:
                # Keep serving history and retry after disconnection; never affect body control.
                with self._lock:
                    self.error = str(exc)
                    self._prune(time.monotonic())
            # No catch-up bursts if encoding or tonyd was slow.
            self._stop.wait(max(0, 1 / self.fps - (time.monotonic() - started)))

    def select(self, frames: int, seconds: float, end_ago_s: float = 0) -> tuple[list[Frame], dict]:
        if isinstance(frames, bool) or not isinstance(frames, int) or not 1 <= frames <= 24:
            raise ValueError("frames must be an integer from 1 to 24")
        if not math.isfinite(seconds) or not 0 < seconds <= self.retention_s:
            raise ValueError("seconds must be greater than 0 and at most 60")
        if not math.isfinite(end_ago_s) or end_ago_s < 0 or end_ago_s + seconds > self.retention_s:
            raise ValueError("end_ago_s must be nonnegative and end_ago_s + seconds at most 60")
        now = time.monotonic()
        end = now - end_ago_s
        with self._lock:
            self._prune(now)
            available = [f for f in self._frames if end - seconds <= f.monotonic_s <= end]
            status = {"sampling_fps": self.fps, "retention_s": self.retention_s,
                      "buffered_frames": len(self._frames), "buffer_bytes": self._bytes,
                      "latest_age_s": round(now - self._frames[-1].monotonic_s, 3) if self._frames else None,
                      "capture_error": self.error}
        count = min(frames, len(available))
        if count == 1:
            chosen = available[-1:]  # one frame means the newest in the requested window
        elif count:
            chosen = [available[round(i * (len(available) - 1) / (count - 1))] for i in range(count)]
        else:
            chosen = []
        status.update({"requested_frames": frames, "returned_frames": count, "window_seconds": seconds,
                       "end_ago_s": end_ago_s, "timestamp_basis": "estimated capture UTC from tonyd frame age; "
                       "includes snapshot encoding and socket latency", "order": "oldest first, row-major",
                       "frames": [{"index": i + 1, "utc": self.utc(f.epoch_s),
                                   "age_s": round(now - f.monotonic_s, 3), "head_deg": f.head_deg}
                                  for i, f in enumerate(chosen)]})
        return chosen, status

    @staticmethod
    def utc(epoch: float) -> str:
        return datetime.fromtimestamp(epoch, timezone.utc).isoformat(timespec="milliseconds")

    def mosaic(self, frames: int = 24, seconds: float = 12, end_ago_s: float = 0) -> tuple[dict, bytes | None]:
        import cv2

        chosen, status = self.select(frames, seconds, end_ago_s)
        if not chosen:
            status["message"] = "No frames in this window (warming up, disconnected, or outside retained history)."
            return status, None
        columns = min(4, len(chosen))
        rows = math.ceil(len(chosen) / columns)
        cell_height = self.tile_height + 24
        sheet = np.zeros((rows * cell_height, columns * self.tile_width, 3), dtype=np.uint8)
        for i, frame in enumerate(chosen):
            x, y = (i % columns) * self.tile_width, (i // columns) * cell_height
            image = cv2.imdecode(np.frombuffer(frame.jpeg, dtype=np.uint8), cv2.IMREAD_COLOR)
            sheet[y + 24:y + cell_height, x:x + self.tile_width] = image
            stamp = datetime.fromtimestamp(frame.epoch_s, timezone.utc).strftime("%H:%M:%S.%f")[:-3]
            cv2.putText(sheet, f"{i + 1:02d} {stamp} UTC", (x + 4, y + 17), cv2.FONT_HERSHEY_SIMPLEX,
                        0.45, (255, 255, 255), 1, cv2.LINE_AA)
        ok, jpeg = cv2.imencode(".jpg", sheet, [cv2.IMWRITE_JPEG_QUALITY, 75])
        if not ok:
            raise ValueError("mosaic JPEG encoding failed")
        status.update({"width": sheet.shape[1], "height": sheet.shape[0], "jpeg_bytes": len(jpeg)})
        return status, jpeg.tobytes()
