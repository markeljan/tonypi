"""Client for the Hermes Agent API server's runs API (POST /v1/runs, SSE events, steer, stop). Standard library only.

A run is one turn of the Hermes agent loop in a named session. While it runs, `steer()` hands Hermes more user text,
which it delivers into the same turn right after the current tool call (no restart, history kept); `stop()` is a hard
interrupt. A steer that arrives after the model's last tool call comes back on the terminal event as `pending_steer`.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Iterator

URL = os.environ.get("HERMES_API_URL", "http://127.0.0.1:8642")
SECRETS = "/home/pi/.config/tonypi/secrets.env"
TERMINAL = {"run.completed", "run.failed", "run.cancelled", "run.interrupted"}


class HermesError(RuntimeError):
    pass


def _key() -> str:
    if key := os.environ.get("HERMES_API_KEY"):
        return key
    try:
        for line in open(SECRETS):
            if line.startswith("HERMES_API_KEY="):
                return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return ""


class HermesRuns:
    def __init__(self, url: str = URL, key: str | None = None) -> None:
        self.url = url.rstrip("/")
        self.key = _key() if key is None else key

    def _req(self, method: str, path: str, body: dict | None = None, timeout: float = 15):
        req = urllib.request.Request(self.url + path, method=method,
                                     data=None if body is None else json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json",
                                              "Authorization": f"Bearer {self.key}"})
        try:
            return urllib.request.urlopen(req, timeout=timeout)
        except urllib.error.HTTPError as e:
            raise HermesError(f"{method} {path}: HTTP {e.code} {e.read()[:300].decode(errors='replace')}") from e
        except OSError as e:
            raise HermesError(f"Hermes API not reachable at {self.url} ({e})") from e

    def health(self) -> bool:
        try:
            with self._req("GET", "/health", timeout=3) as r:
                return r.status == 200
        except HermesError:
            return False

    def start(self, text: str, session_id: str, model: str = "", provider: str = "", effort: str = "") -> str:
        """model/provider/effort: per-run choice (else the profile's default model and agent.reasoning_effort)."""
        body: dict = {"input": text, "session_id": session_id}
        if model:
            body.update(model=model, provider=provider)
        if effort:
            body["model_options"] = {"reasoning_effort": effort}
        with self._req("POST", "/v1/runs", body) as r:
            return json.load(r)["run_id"]

    def events(self, run_id: str) -> Iterator[dict]:
        """SSE stream of the run; ends after the terminal event (run.completed / failed / cancelled)."""
        with self._req("GET", f"/v1/runs/{run_id}/events", timeout=600) as r:
            data: list[str] = []
            for raw in r:
                line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                if line.startswith("data:"):
                    data.append(line[5:].lstrip())
                elif not line and data:
                    try:
                        ev = json.loads("\n".join(data))
                    except json.JSONDecodeError:
                        ev = None
                    data = []
                    if isinstance(ev, dict):
                        yield ev
                        if ev.get("event") in TERMINAL:
                            return

    def steer(self, run_id: str, text: str) -> bool:
        """True if the running turn took the text; False if it already ended (start a new run instead)."""
        try:
            with self._req("POST", f"/v1/runs/{run_id}/steer", {"input": text}) as r:
                return bool(json.load(r).get("accepted"))
        except HermesError:
            return False

    def stop(self, run_id: str) -> None:
        try:
            self._req("POST", f"/v1/runs/{run_id}/stop", {}).close()
        except HermesError:
            pass
