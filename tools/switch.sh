#!/bin/bash
# Switch Tony's brain: tools/switch.sh live | local | status   (src/tonypi/switch.py does the same by voice:
# "switch to local" / "switch to live"). live = tonypi-voice (GPT-Live + Codex brain, the default at boot);
# local = tonypi-agent (offline LFM3 fallback). The units conflict, so starting one stops the other.
set -u
target=${1:-status}
running() { systemctl is-active -q tonypi-voice && echo live; systemctl is-active -q tonypi-agent && echo local; }
case $target in
  status) cur=$(running); echo "running: ${cur:-none}"; exit 0 ;;
  live|own|codex) unit=tonypi-voice; target=live ;;
  local|off|default) unit=tonypi-agent; target=local ;;
  *) echo "usage: $0 live|local|status"; exit 2 ;;
esac
[[ $(running) == "$target" ]] && { echo "$target is already running"; exit 0; }
sudo systemctl start "$unit"
echo "running: $target (journal: journalctl -fu $unit)"
