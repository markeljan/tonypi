"""Camera history tests use generated images and a fake tonyd, never the robot."""

import asyncio
import json
from types import SimpleNamespace
import threading

import cv2
import numpy as np
import pytest

from tonypi import video_context
from tonypi.video_context import FrameHistory


@pytest.fixture
def history(tmp_path, monkeypatch):
    clock = SimpleNamespace(now=100.0)
    monkeypatch.setattr(video_context, "time", SimpleNamespace(monotonic=lambda: clock.now,
                                                            time=lambda: 1700000000 + clock.now))
    path = tmp_path / "shot.jpg"
    cv2.imwrite(str(path), np.full((480, 640, 3), (20, 80, 160), dtype=np.uint8))
    shot = {"path": str(path), "frame_age_s": 0.02, "head_deg": {"pan": 10, "tilt": 5}}
    return FrameHistory(lambda: shot), clock, shot


def fill(history, count):
    h, clock, _ = history
    for _ in range(count):
        assert h.capture()
        clock.now += 0.5
    return h, clock


def test_rolling_bounds_and_estimated_capture_time(history):
    h, clock = fill(history, 150)
    selected, status = h.select(24, 60)
    assert status["buffered_frames"] == 119  # expired oldest at query time
    assert status["buffer_bytes"] < h.max_bytes
    assert len(selected) == 24
    assert selected[0].monotonic_s < selected[-1].monotonic_s
    assert len({f.monotonic_s for f in selected}) == 24
    assert selected[-1].epoch_s == pytest.approx(1700000000 + clock.now - 0.52)
    assert status["frames"][-1]["age_s"] == 0.52
    assert status["frames"][-1]["head_deg"] == {"pan": 10, "tilt": 5}
    assert status["frames"][-1]["utc"].endswith("+00:00")


def test_window_selection_and_one_frame(history):
    h, clock = fill(history, 40)
    selected, status = h.select(5, seconds=5, end_ago_s=10)
    assert len(selected) == 5
    assert all(clock.now - 15 <= f.monotonic_s <= clock.now - 10 for f in selected)
    assert selected[0].monotonic_s == pytest.approx(105.48)
    assert selected[-1].monotonic_s == pytest.approx(109.98)
    latest, _ = h.select(1, 5, 10)
    assert latest == selected[-1:]
    assert status["end_ago_s"] == 10


def test_memory_cap_can_shorten_retention(history):
    h, _, _ = history
    h.max_bytes = 6000
    h, _ = fill(history, 10)
    _, status = h.select(24, 60)
    assert status["buffer_bytes"] <= 6000
    assert 0 < status["buffered_frames"] < 10


def test_mosaic_order_and_dimensions(history):
    h, clock, shot = history
    for i in range(24):
        cv2.imwrite(shot["path"], np.full((480, 640, 3), i * 10, dtype=np.uint8))
        h.capture()
        clock.now += 0.5
    status, jpeg = h.mosaic(24, 13)
    image = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
    assert image.shape == (1584, 1280, 3)
    assert status["jpeg_bytes"] == len(jpeg)
    assert status["returned_frames"] == 24
    # Actual tile pixels establish chronological row-major order, independent of metadata.
    for i in range(24):
        pixel = image[(i // 4) * 264 + 100, (i % 4) * 320 + 100]
        assert np.allclose(pixel, i * 10, atol=3)
    assert image[:24].max() > 0  # timestamp label drawn


def test_warmup_empty_and_expiry(history):
    h, clock, _ = history
    status, jpeg = h.mosaic()
    assert jpeg is None and status["returned_frames"] == 0
    h.capture()
    status, jpeg = h.mosaic()
    assert jpeg and status["returned_frames"] == 1
    clock.now += 61
    status, jpeg = h.mosaic()
    assert jpeg is None and status["buffer_bytes"] == 0


@pytest.mark.parametrize("frames,seconds,end", [(0, 12, 0), (25, 12, 0), (True, 12, 0), (2.5, 12, 0),
                                                  (24, 0, 0), (24, 61, 0), (24, float("nan"), 0),
                                                  (24, 12, -1), (24, 12, 49), (24, 12, float("inf"))])
def test_invalid_requests(history, frames, seconds, end):
    with pytest.raises(ValueError):
        history[0].mosaic(frames, seconds, end)


def test_stale_duplicate_and_bad_camera(history):
    h, clock, shot = history
    assert h.capture()
    assert not h.capture()
    clock.now += 0.5
    shot["frame_age_s"] = 1
    with pytest.raises(ValueError, match="stale"):
        h.capture()
    shot["frame_age_s"] = 0.01
    with open(shot["path"], "wb") as out:
        out.write(b"bad jpeg")
    with pytest.raises(ValueError, match="invalid"):
        h.capture()
    assert len(h._frames) == 1


def test_background_recovers_and_stops(tmp_path):
    path = tmp_path / "shot.jpg"
    cv2.imwrite(str(path), np.zeros((10, 10, 3), np.uint8))
    recovered = threading.Event()
    attempts = []

    def snapshot():
        attempts.append(1)
        if len(attempts) == 1:
            raise OSError("camera disconnected")
        recovered.set()
        return {"path": str(path), "frame_age_s": 0}

    h = FrameHistory(snapshot)
    h.fps = 20
    h.start()
    h.start()  # idempotent, only one sampler
    try:
        assert recovered.wait(2)
    finally:
        h.stop()
    assert not h._thread.is_alive()
    assert h.error is None and len(h._frames) == 1


def test_mcp_tool_returns_one_image_and_lifecycle(history, monkeypatch):
    from mcp.server.mcpserver.exceptions import ToolError
    from mcp.server.mcpserver.utilities.types import Image
    from tonypi import mcp_server

    h, _ = fill(history, 24)
    monkeypatch.setattr(mcp_server, "frame_history", h)
    result = mcp_server.video_context(24, 13)
    assert len(result) == 2 and isinstance(result[1], Image)
    assert json.loads(result[0])["returned_frames"] == 24
    with pytest.raises(ToolError, match="frames"):
        mcp_server.video_context(25)

    events = []
    monkeypatch.setattr(h, "start", lambda: events.append("start"))
    monkeypatch.setattr(h, "stop", lambda: events.append("stop"))
    monkeypatch.setattr(mcp_server.context_camera, "close", lambda: events.append("close"))

    async def lifecycle():
        async with mcp_server._lifespan(mcp_server.mcp):
            assert events == ["start"]

    asyncio.run(lifecycle())
    assert events == ["start", "stop", "close"]
