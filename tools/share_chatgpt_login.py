"""Share the one ChatGPT (Codex) login made in Hermes with pi and the Codex CLI (harness bake-off, 2026-10-29).

Hermes (profile `tonypi`) is the only owner and the only refresher: pi and Codex get the current access token
(valid ~10 days) but NOT the refresh token, so they can't rotate it and log Hermes out. Re-run (the
tonypi-chatgpt-share timer does it hourly) to pick up a token Hermes has refreshed. If every token has expired,
log in again in Hermes:  tonypi auth add openai-codex --type oauth --no-browser
"""

from __future__ import annotations

import base64
import json
import os
import sys
import time
from pathlib import Path

HERMES = Path("/home/pi/.hermes/profiles/tonypi/auth.json")
PI = Path("/home/pi/.pi/agent/auth.json")
CODEX_HOMES = [Path("/home/pi/.config/tonypi/codex-own")]
NO_REFRESH = "shared-from-hermes-no-refresh"  # deliberately invalid: only Hermes may refresh


def claims(jwt: str) -> dict:
    return json.loads(base64.urlsafe_b64decode(jwt.split(".")[1] + "=="))


def write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    os.chmod(tmp, 0o600)
    tmp.replace(path)


def main() -> int:
    pool = json.loads(HERMES.read_text())["credential_pool"].get("openai-codex") or []
    if not pool:
        print(f"no openai-codex login in {HERMES}", file=sys.stderr)
        return 1
    cred = pool[0]
    access = cred["access_token"]
    c = claims(access)
    exp, account = c["exp"], c["https://api.openai.com/auth"]["chatgpt_account_id"]
    hours = (exp - time.time()) / 3600
    if hours <= 0:
        print("Hermes's token has expired: log in again in Hermes", file=sys.stderr)
        return 1

    auth = json.loads(PI.read_text()) if PI.exists() else {}
    auth["openai-codex"] = {"type": "oauth", "access": access, "refresh": NO_REFRESH,
                            "expires": exp * 1000 - 60_000, "accountId": account}
    write(PI, auth)

    for home in CODEX_HOMES:
        write(home / "auth.json", {
            "OPENAI_API_KEY": None,
            "tokens": {"id_token": access, "access_token": access, "refresh_token": NO_REFRESH,
                       "account_id": account},
            "last_refresh": cred.get("last_refresh") or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        })
    print(f"shared Hermes's ChatGPT token with pi + {len(CODEX_HOMES)} Codex home(s); valid {hours:.0f} more hours")
    return 0


if __name__ == "__main__":
    sys.exit(main())
