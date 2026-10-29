"""tonyd: the body daemon (single owner of the controller board and camera). See server.py."""

import os

SOCK = os.environ.get("TONYD_SOCK", "/run/tonypi/tonyd.sock")
