#!/bin/bash
# Switch Tony's harness: tools/switch.sh own | hermes | pi | off | status
# (Voice: "switch to pi" / "switch to Hermes" / "switch to our own" / "switch to local".)
set -u
target=${1:-status}
running() { for n in own hermes pi; do systemctl is-active -q "tonypi-brain@$n" && echo "$n"; done; }
if [[ $target == status ]]; then cur=$(running); echo "running: ${cur:-off (tonypi-agent)}"; exit 0; fi
[[ $target =~ ^(own|hermes|pi|off)$ ]] || { echo "usage: $0 own|hermes|pi|off|status"; exit 2; }
for n in $(running); do
  [[ $n == "$target" ]] && { echo "$n is already running"; exit 0; }
  echo "stopping $n"; sudo systemctl stop "tonypi-brain@$n"
done
if [[ $target == off ]]; then
  sudo systemctl start tonypi-agent
  echo "running: off (tonypi-agent)"
else
  sudo systemctl start "tonypi-brain@$target"
  echo "running: $target (journal: journalctl -fu tonypi-brain@$target)"
fi
