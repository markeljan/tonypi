#!/bin/bash
# The Hermes harness, one command (E2E test and everyday use):
#   hermes/start.sh [--no-voice] [--local-voice]
# Holds the robot lock for the whole run. Starts tonypi-voice (GPT-Live-1; its unit stops tonypi-agent, the usual
# owner of mic, speaker and agent.sock) and attaches to it as the active brain; --local-voice: our own wake word +
# Piper instead (tonypi-agent stopped); --no-voice: typed only. Syncs the Hermes profile, starts the Hermes gateway
# (user unit tonypi-hermes-gw, capped at 1.2 GB) and the front end in the foreground with the body keeper. Ctrl-C
# ends it and starts tonypi-agent again (which stops tonypi-voice). Typed: tonypi-chat "walk to the chair".
# Tasks are logged to ~/.local/state/tonypi/hermes/tasks.jsonl; the front-end log goes to the terminal.
set -u
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY=/home/pi/tonypi/.venv/bin/python
LOCK=/run/lock/tonypi-robot.lock

# under a system unit (tonypi-brain@hermes, User=pi) there is no login session: point systemctl --user at pi's manager
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
export DBUS_SESSION_BUS_ADDRESS="${DBUS_SESSION_BUS_ADDRESS:-unix:path=$XDG_RUNTIME_DIR/bus}"
USER_UNITS=1
systemctl --user list-units >/dev/null 2>&1 || USER_UNITS=0

exec 9>"$LOCK"
echo "waiting for the robot lock ($LOCK)..."
flock 9
cleanup() {
    echo "stopping the Hermes harness"
    [ -n "${FRONT:-}" ] && kill "$FRONT" 2>/dev/null && wait "$FRONT" 2>/dev/null
    if [ "$USER_UNITS" = 1 ]; then systemctl --user stop tonypi-hermes-gw 2>/dev/null
    elif [ -n "${GW:-}" ]; then kill "$GW" 2>/dev/null; wait "$GW" 2>/dev/null; fi
    sudo systemctl start tonypi-agent   # stops tonypi-voice (Conflicts=)
}
trap cleanup EXIT INT TERM

MODE=voice-service
for a in "$@"; do case "$a" in --no-voice) MODE=none ;; --local-voice) MODE=local ;; esac; done
FRONT_ARGS=()
case "$MODE" in
    voice-service)
        sudo systemctl start tonypi-voice   # stops tonypi-agent
        "$PY" -c "
import socket, time
for _ in range(40):  # the service takes a few seconds to accept connections
    try:
        s = socket.socket(socket.AF_UNIX); s.settimeout(5); s.connect('/run/tonypi/voice.sock')
        s.sendall(b'{\"op\": \"status\"}\n'); print('tonypi-voice:', s.makefile().readline().strip()); break
    except OSError:
        time.sleep(0.5)
else:
    print('tonypi-voice: not answering')"
        FRONT_ARGS=(--voice-service) ;;
    local) sudo systemctl stop tonypi-agent ;;
    none) sudo systemctl stop tonypi-agent; FRONT_ARGS=(--no-voice) ;;
esac
"$REPO/hermes/setup.sh" --brain codex >/dev/null || exit 1
if [ "$USER_UNITS" = 1 ]; then
    systemctl --user stop tonypi-hermes-gw 2>/dev/null; systemctl --user reset-failed tonypi-hermes-gw 2>/dev/null
    systemd-run --user --unit=tonypi-hermes-gw -q -p MemoryMax=1200M -p MemorySwapMax=0 -p Restart=on-failure \
        -p RestartSec=15 /home/pi/.local/bin/tonypi gateway run
else  # no user manager (nobody logged in, linger off): the gateway is our child, in this unit's cgroup
    echo "no systemd user manager: running the Hermes gateway as a child process"
    /home/pi/.local/bin/tonypi gateway run --external-supervisor &
    GW=$!
fi
for i in $(seq 120); do curl -sf http://127.0.0.1:8642/health >/dev/null && break; sleep 1; done
sleep 8  # MCP servers connect after the API is up

# E2E procedure: reset the tether's walking budget at the start of every task
TONYPI_HERMES_TETHER_RESET="${TONYPI_HERMES_TETHER_RESET:-1}" PYTHONPATH="$REPO/src" \
    "$PY" -m tonypi.hermes.front "${FRONT_ARGS[@]}" &
FRONT=$!
sleep 3
PYTHONPATH="$REPO/src" "$PY" "$REPO/tools/hermes_ready.py" || true
wait "$FRONT"
