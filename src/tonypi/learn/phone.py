"""A small client for the phone bridge's MCP server (iPeripheral, http://127.0.0.1:8767/mcp): the outside observer.

Everything is optional: no bridge, no phone or a failing call means trials are scored without the phone. Recordings
are read back from the bridge's episode folders on this Pi (frames.jsonl: the tracked world position per frame).
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import math
import os
from pathlib import Path

log = logging.getLogger(__name__)

PHONE_MCP = os.environ.get("TONYPI_PHONE_MCP", "http://127.0.0.1:8767/mcp")
EPISODES = Path(os.environ.get("TONYPI_PHONE_EPISODES", Path.home() / ".local/share/tonypi-phone/episodes"))
TIMEOUT_S = 20.0
TRACK_TIMEOUT_S = 45.0  # phone_track finds the target with LFM3-VL first


class PhoneError(RuntimeError):
    pass


class Phone:
    def __init__(self, url: str = PHONE_MCP, episodes: Path = EPISODES) -> None:
        self.url, self.episodes = url, Path(episodes)

    def call(self, tool: str, timeout: float = TIMEOUT_S, **args) -> dict:
        """One MCP tool call (a fresh session each time: calls are seconds apart, and the bridge may restart)."""
        try:
            return asyncio.run(asyncio.wait_for(self._call(tool, args), timeout))
        except PhoneError:
            raise
        except Exception as e:  # connection refused, timeout, protocol errors: the phone is optional
            raise PhoneError(f"phone {tool}: {type(e).__name__}: {e}") from e

    async def _call(self, tool: str, args: dict) -> dict:
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client

        async with streamable_http_client(self.url) as (read, write), ClientSession(read, write) as s:
            await s.initialize()
            r = await s.call_tool(tool, {k: v for k, v in args.items() if v is not None})
        text = " ".join(getattr(c, "text", "") for c in r.content or [])
        if r.is_error:
            raise PhoneError(f"phone {tool}: {text or 'error'}")
        if isinstance(r.structured_content, dict):
            sc = r.structured_content
            return sc["result"] if set(sc) == {"result"} and isinstance(sc["result"], dict) else sc
        try:
            return json.loads(text) if text else {}
        except ValueError:
            return {"text": text}

    # ---- the few things a trial needs --------------------------------------------------------------------------

    def status(self) -> dict:
        return self.call("phone_status", timeout=8)

    def available(self) -> bool:
        try:
            return bool(self.status().get("connected"))
        except PhoneError:
            return False

    def mode(self) -> str | None:
        """'companion' (on a stand watching Tony), 'mounted', or None when unknown / unreachable."""
        try:
            st = self.status()
            if not st.get("connected"):
                return None
            return st.get("mode") or self.call("phone_mode", timeout=8).get("mode")
        except PhoneError:
            return None

    def track(self, target: str = "the small humanoid robot") -> dict:
        return self.call("phone_track", timeout=TRACK_TIMEOUT_S, target=target)

    def record_start(self, label: str, max_s: float = 60) -> dict:
        return self.call("phone_record", action="start", label=label, max_s=max(5.0, min(600.0, float(max_s))))

    def record_stop(self) -> dict:
        return self.call("phone_record", action="stop")

    def episode(self, episode_id: str) -> dict:
        return episode_summary(self.episodes / Path(str(episode_id)).name)


def episode_summary(folder: Path, max_points: int = 40) -> dict:
    """What the tracked target did in an episode: net floor displacement (x/z; y is up in ARKit), path length, and
    the path itself (t from the first tracked frame, x, z; at most max_points). Robust to a missing or torn file."""
    out: dict = {"id": folder.name, "frames": 0, "frames_tracked": 0}
    pts: list[tuple[float, float, float]] = []
    try:
        lines = (folder / "frames.jsonl").read_text().splitlines()
    except OSError:
        return {**out, "error": "no frames.jsonl"}
    for line in lines:
        try:
            fr = json.loads(line)
        except ValueError:
            continue
        out["frames"] += 1
        tr = fr.get("tracked") or {}
        w = tr.get("world")
        if w and not tr.get("lost") and len(w) >= 3:
            pts.append((float(fr.get("rx") or 0), float(w[0]), float(w[2])))
    out["frames_tracked"] = len(pts)
    if len(pts) >= 2:
        t0, x0, z0 = pts[0]
        _, x1, z1 = pts[-1]
        out["target_moved_m"] = round(math.hypot(x1 - x0, z1 - z0), 3)
        out["path_m"] = round(sum(math.hypot(b[1] - a[1], b[2] - a[2]) for a, b in itertools.pairwise(pts)), 3)
        out["seconds_tracked"] = round(pts[-1][0] - t0, 2)
        step = max(1, math.ceil(len(pts) / max_points))
        out["path"] = [[round(t - t0, 2), round(x, 3), round(z, 3)] for t, x, z in pts[::step]]
    return out
