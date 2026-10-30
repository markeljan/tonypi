#!/bin/bash
# Start Tony with the pi brain (bake-off option 3): voice + typed commands (/run/tonypi/pi.sock).
#   pi/start.sh             voice and text
#   pi/start.sh --no-voice  typed commands only (speaker still on)
# Type commands from another terminal:  TONYPI_SOCK=/run/tonypi/pi.sock /home/pi/tonypi/.venv/bin/tonypi-chat
# During a test slot the owner holds the robot lock and stops tonypi-agent (it owns the mic and speaker).
set -euo pipefail
cd "$(dirname "$0")/.."
fail() { echo "not ready: $*" >&2; exit 1; }

systemctl is-active -q tonypi-tonyd || fail "tonyd is not running (sudo systemctl start tonypi-tonyd)"
pi auth check --provider openai-codex </dev/null 2>/dev/null | grep -qx ready || fail "ChatGPT login (openai-codex) not ready"
pi --offline --list-models gpt-6.1-terra </dev/null 2>/dev/null | grep -q "gpt-6.1-terra" || pi update --models </dev/null >/dev/null
if [[ " $* " != *" --no-voice "* ]] && systemctl is-active -q tonypi-agent; then
  fail "tonypi-agent owns the mic and speaker: sudo systemctl stop tonypi-agent (start it again afterwards)"
fi
ln -sf "$PWD/pi/models.json" ~/.pi/agent/models.json
ln -sf "$PWD/pi/settings.json" ~/.pi/agent/settings.json
echo "Tony (pi brain) on $(git rev-parse --abbrev-ref HEAD) @ $(git rev-parse --short HEAD); log: ~/.local/state/tonypi/pi-front.jsonl"
PYTHONPATH="$PWD/src" exec /home/pi/tonypi/.venv/bin/python -m tonypi.pi_front.daemon "$@"
