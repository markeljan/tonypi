"""Camera peripheral: a photo, or a short burst of frames as "video" (tonyd owns the camera)."""

from __future__ import annotations

import time
from pathlib import Path

from tonypi.harness.peripherals import Ctx, Peripheral, Result, Tool
from tonypi.tonyd.client import TonydClient

MAX_FRAMES = 6


def shrink(jpeg: bytes, width: int = 320) -> bytes:
    """Smaller frames for bursts (fewer image tokens). Falls back to the original without OpenCV."""
    try:
        import cv2
        import numpy as np
    except ImportError:
        return jpeg
    img = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
    if img is None or img.shape[1] <= width:
        return jpeg
    img = cv2.resize(img, (width, round(img.shape[0] * width / img.shape[1])), interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 80])
    return buf.tobytes() if ok else jpeg


class CameraPeripheral(Peripheral):
    name = "camera"
    prompt = ("Camera: in Tony's head, about 30 cm above the floor, 640x480, ~60 degree field of view; it points "
              "where the head points. snapshot = one photo now; watch = a few smaller frames over a few seconds "
              "(motion, people moving). Look before you claim what is around you.")

    def __init__(self, tonyd: TonydClient) -> None:
        self.tonyd = tonyd

    def tools(self) -> list[Tool]:
        return [
            Tool("snapshot", "Take a photo with the head camera right now (640x480 JPEG).", self.snapshot,
                 ordered=True),
            Tool("watch", "Short video: `frames` photos (2-6, 320x240) spread over `seconds` (1-5).", self.watch,
                 {"seconds": {"type": "number", "minimum": 1, "maximum": 5},
                  "frames": {"type": "integer", "minimum": 2, "maximum": MAX_FRAMES}}, ordered=True),
        ]

    def grab(self) -> tuple[bytes, dict]:
        snap = self.tonyd.snapshot()
        return Path(snap["path"]).read_bytes(), snap

    def snapshot(self, args: dict, ctx: Ctx) -> Result:
        jpeg, snap = self.grab()
        h = snap["head_deg"]
        return Result(f"photo, head pan {h['pan']:.0f} tilt {h['tilt']:.0f}", [jpeg])

    def watch(self, args: dict, ctx: Ctx) -> Result:
        seconds = max(1.0, min(5.0, float(args.get("seconds") or 2)))
        n = max(2, min(MAX_FRAMES, int(args.get("frames") or 4)))
        frames, t0 = [], time.monotonic()
        for i in range(n):
            wait = t0 + i * seconds / (n - 1) - time.monotonic()
            if wait > 0 and ctx.steer.wait(wait):
                break
            frames.append(shrink(self.grab()[0]))
        return Result(f"{len(frames)} frames over {time.monotonic() - t0:.1f} s, oldest first", frames)
