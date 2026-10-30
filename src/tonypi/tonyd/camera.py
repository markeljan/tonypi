"""Always-on camera: keeps the latest frame so any snapshot is instant. Survives the camera being unplugged."""

from __future__ import annotations

import glob
import logging
import os
import threading
import time
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)


def find_device() -> str | None:
    if dev := os.environ.get("TONYPI_CAMERA"):
        return dev
    # the UVC camera; /dev/video19+ on the Pi 5 are codec devices, not cameras
    by_id = sorted(glob.glob("/dev/v4l/by-id/usb-*-video-index0"))
    if by_id:
        return by_id[0]
    return "/dev/video0" if os.path.exists("/dev/video0") else None


class Camera:
    def __init__(self, width: int = 640, height: int = 480, fps: int = 15) -> None:
        self.width, self.height, self.fps = width, height, fps
        self.device: str | None = None
        self._frame = None
        self._t = 0.0
        self._lock = threading.Lock()
        self._running = False
        self._missing_logged = False
        self.frames = 0
        self.connected: bool | None = None  # None until the first open attempt
        self.on_change: Callable[[bool], None] | None = None  # called when the camera is lost / found again

    def _set_connected(self, ok: bool) -> None:
        prev, self.connected = self.connected, ok
        if ok == prev or (prev is None and ok) or self.on_change is None:  # opening fine at startup is no news
            return
        try:
            self.on_change(ok)
        except Exception:
            log.exception("camera on_change failed")

    def start(self) -> "Camera":
        self._running = True
        threading.Thread(target=self._loop, name="camera", daemon=True).start()
        return self

    def stop(self) -> None:
        self._running = False

    def _open(self):
        import cv2

        self.device = find_device()
        if self.device is None:
            return None
        cap = cv2.VideoCapture(self.device, cv2.CAP_V4L2)
        if not cap.isOpened():
            return None
        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"YUYV"))
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
        cap.set(cv2.CAP_PROP_FPS, self.fps)
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        return cap

    def _loop(self) -> None:
        try:
            import cv2  # noqa: F401
        except ImportError:
            log.error("opencv not installed (uv sync --extra vision): camera disabled")
            return
        cap, failures = None, 0
        while self._running:
            if cap is None:
                cap = self._open()
                if cap is None:
                    self._set_connected(False)  # the device is gone (a reopen that works at once is not a loss)
                    if not self._missing_logged:
                        log.warning("no camera found (is the USB cable seated?); retrying every 3 s")
                        self._missing_logged = True
                    time.sleep(3)
                    continue
                log.info("camera open: %s", self.device)
                self._set_connected(True)
                self._missing_logged, failures = False, 0
            ok, frame = cap.read()
            if not ok:
                failures += 1
                if failures > 10:
                    log.warning("camera stopped delivering frames; reopening")
                    cap.release()
                    cap = None
                    with self._lock:
                        self._frame = None
                time.sleep(0.1)
                continue
            failures = 0
            with self._lock:
                self._frame, self._t = frame, time.monotonic()
            self.frames += 1
        if cap is not None:
            cap.release()

    def latest(self, max_age: float = 2.0):
        """(frame, monotonic time) or None if there is no frame younger than max_age."""
        with self._lock:
            if self._frame is None or time.monotonic() - self._t > max_age:
                return None
            return self._frame, self._t

    def jpeg(self, quality: int = 85, max_age: float = 2.0, newer_than: float | None = None) -> tuple[bytes, float] | None:
        """newer_than: wait (up to 1.5 s) for a frame captured after that monotonic time, e.g. after a head move."""
        import cv2

        deadline = time.monotonic() + 1.5
        while newer_than is not None and self._t <= newer_than and time.monotonic() < deadline:
            time.sleep(0.02)
        got = self.latest(max_age)
        if got is None:
            return None
        frame, t = got
        ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, quality])
        return (buf.tobytes(), t) if ok else None

    def snapshot(self, path: Path, quality: int = 85, newer_than: float | None = None) -> float | None:
        """Writes the latest frame as JPEG (atomically). Returns the frame's age in seconds, or None."""
        got = self.jpeg(quality, newer_than=newer_than)
        if got is None:
            return None
        data, t = got
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(data)
        tmp.replace(path)
        return time.monotonic() - t

    def status(self) -> dict:
        with self._lock:
            age = None if self._frame is None else round(time.monotonic() - self._t, 2)
        return {"ok": age is not None and age < 2.0, "device": self.device, "frame_age_s": age,
                "width": self.width, "height": self.height}
