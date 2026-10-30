#!/bin/bash
# Final E2E test: start one harness for its slot, the same way for all three (docs/E2E_TEST.md).
#   tools/e2e.sh own | hermes | pi
# Runs in the foreground; Ctrl-C ends the slot and hands the robot back (tonypi-agent restarts, tonypi-voice stops).
# Before each slot this centres the head and stands Tony up (settle); place him at the start mark facing the wall.
# Give tasks by voice ("Tony, walk to the chair") or typed from a second terminal (printed below).
set -u
W=/home/pi/tonypi/.claude/worktrees
PY=/home/pi/tonypi/.venv/bin/python
name=${1:-}

prep() {  # same start pose for every harness: head centred, standing, tether off, walking budget reset
  $PY - <<'EOF'
from tonypi.tonyd.client import TonydClient
c = TonydClient()
c.head(0, 0)
st = c.state(fresh=True)
c.run("get_up" if st["lying"] else "settle")
c.call("tether", on=False, reset=True)
st = c.state(fresh=True)
print(f"start pose: tilt {st['tilt_deg']} deg, lying {st['lying']}, battery {st['battery_mv'] / 1000:.1f} V, "
      f"camera {'ok' if st['camera']['ok'] else 'MISSING'}")
EOF
}

case "$name" in
  own)
    echo "typed commands: (cd $W/harness-own-817e6a && .venv/bin/tonypi-harness-chat)"
    flock /run/lock/tonypi-robot.lock bash -c "$(declare -f prep); PY=$PY prep"  # the slot script takes the lock next
    cd $W/harness-own-817e6a && exec tools/own_slot.sh ;;
  hermes)
    echo 'typed commands: /home/pi/tonypi/.venv/bin/tonypi-chat "walk to the chair"'
    flock /run/lock/tonypi-robot.lock bash -c "$(declare -f prep); PY=$PY prep"
    cd $W/harness-hermes-e3daa7 && exec hermes/start.sh ;;
  pi)
    echo "typed commands: TONYPI_SOCK=/run/tonypi/pi.sock /home/pi/tonypi/.venv/bin/tonypi-chat"
    exec 9>/run/lock/tonypi-robot.lock
    flock 9
    trap 'sudo systemctl start tonypi-agent' EXIT INT TERM
    prep
    sudo systemctl start tonypi-voice   # stops tonypi-agent (the units conflict)
    for _ in $(seq 30); do
      $PY -c "import json,socket; s=socket.socket(socket.AF_UNIX); s.connect('/run/tonypi/voice.sock'); s.sendall(b'{\"op\":\"status\",\"rid\":1}\n'); exit(0 if json.loads(s.makefile().readline())['mode']=='gpt-live' else 1)" 2>/dev/null && break
      sleep 1
    done
    cd $W/harness-pi-9ec65f && pi/start.sh ;;
  *)
    echo "usage: tools/e2e.sh own|hermes|pi"; exit 2 ;;
esac
