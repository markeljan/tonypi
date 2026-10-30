#!/bin/bash
# The Hermes harness, one command (E2E test and everyday use):
#   hermes/start.sh [--no-voice]
# Holds the robot lock for the whole run, stops tonypi-agent (mic/speaker/agent.sock owner), syncs the Hermes profile,
# starts the Hermes gateway (user unit tonypi-hermes-gw, capped at 1.2 GB) and the front end in the foreground with
# the body keeper. Ctrl-C ends it and starts tonypi-agent again. Typed commands: tonypi-chat "walk to the chair".
# Tasks are logged to ~/.local/state/tonypi/hermes/tasks.jsonl; the front-end log goes to the terminal.
set -u
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY=/home/pi/tonypi/.venv/bin/python
LOCK=/run/lock/tonypi-robot.lock

exec 9>"$LOCK"
echo "waiting for the robot lock ($LOCK)..."
flock 9
cleanup() {
    echo "stopping the Hermes harness"
    [ -n "${FRONT:-}" ] && kill "$FRONT" 2>/dev/null && wait "$FRONT" 2>/dev/null
    systemctl --user stop tonypi-hermes-gw 2>/dev/null
    sudo systemctl start tonypi-agent
}
trap cleanup EXIT INT TERM

sudo systemctl stop tonypi-agent
"$REPO/hermes/setup.sh" --brain codex >/dev/null || exit 1
systemctl --user stop tonypi-hermes-gw 2>/dev/null; systemctl --user reset-failed tonypi-hermes-gw 2>/dev/null
systemd-run --user --unit=tonypi-hermes-gw -q -p MemoryMax=1200M -p MemorySwapMax=0 -p Restart=on-failure \
    -p RestartSec=15 /home/pi/.local/bin/tonypi gateway run
for i in $(seq 120); do curl -sf http://127.0.0.1:8642/health >/dev/null && break; sleep 1; done
sleep 8  # MCP servers connect after the API is up

# E2E procedure: reset the tether's walking budget at the start of every task
TONYPI_HERMES_TETHER_RESET="${TONYPI_HERMES_TETHER_RESET:-1}" PYTHONPATH="$REPO/src" \
    "$PY" -m tonypi.hermes.front "$@" &
FRONT=$!
sleep 3
PYTHONPATH="$REPO/src" "$PY" "$REPO/tools/hermes_ready.py" || true
wait "$FRONT"
