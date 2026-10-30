#!/bin/bash
# harness-own's E2E slot, one command: holds the robot lock, gives the harness the mic and speaker (stops
# tonypi-agent), resets the tether budget, checks readiness, runs the harness with voice + the text socket, and
# hands everything back when it exits (Ctrl-C).
#   tools/own_slot.sh            voice + typed (tonypi-harness-chat in another terminal)
#   tools/own_slot.sh --mute     typed only, no speaker
set -u
cd "$(dirname "$0")/.."
exec 9>/run/lock/tonypi-robot.lock
echo "waiting for the robot lock..."
flock 9
restore() { sudo systemctl start tonypi-agent; echo "tonypi-agent: $(systemctl is-active tonypi-agent)"; }
trap restore EXIT
sudo systemctl stop tonypi-agent
.venv/bin/python -c "from tonypi.tonyd.client import TonydClient as T; print('tether:', T().call('tether', reset=True)['tether'])"
.venv/bin/tonypi-harness --check || echo "(readiness: see the unchecked items above)"
args=("$@")
[[ " ${args[*]:-} " == *" --mute "* ]] || args+=(--voice)
TONYPI_MOVE_SUFFIX=_own .venv/bin/tonypi-harness "${args[@]}"
