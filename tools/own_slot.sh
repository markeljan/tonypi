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
# voice: the shared tonypi-voice service (GPT-Live-1) owns mic + speaker for the slot; starting it stops
# tonypi-agent (the units conflict), starting tonypi-agent again at the end stops it.
restore() { sudo systemctl start tonypi-agent; echo "tonypi-agent: $(systemctl is-active tonypi-agent)"; }
trap restore EXIT
if [[ " $* " != *" --mute "* ]]; then
  sudo systemctl start tonypi-voice
  for i in $(seq 30); do
    mode=$(.venv/bin/python -c "import json,socket; s=socket.socket(socket.AF_UNIX); s.connect('/run/tonypi/voice.sock'); s.sendall(b'{\"op\":\"status\",\"rid\":1}\\n'); print(json.loads(s.makefile().readline()).get('mode'))" 2>/dev/null)
    [ -n "$mode" ] && break; sleep 0.5
  done
  echo "tonypi-voice: $(systemctl is-active tonypi-voice), mode ${mode:-?}"
fi
.venv/bin/python -c "from tonypi.tonyd.client import TonydClient as T; print('tether:', T().call('tether', reset=True)['tether'])"
.venv/bin/tonypi-harness --check || echo "(readiness: see the unchecked items above)"
args=("$@")
[[ " ${args[*]:-} " == *" --mute "* ]] || args+=(--voice)
TONYPI_MOVE_SUFFIX=_own .venv/bin/tonypi-harness "${args[@]}"
