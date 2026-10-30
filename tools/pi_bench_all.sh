#!/bin/bash
# bake-off option 3 benchmarks; run under the robot lock:  flock -w 3000 /run/lock/tonypi-robot.lock run.sh [local|deadcloud|cloud]
cd "$(dirname "$0")/.."
export PYTHONPATH=$PWD/src
PY=/home/pi/tonypi/.venv/bin/python
OUT=/home/pi/.local/state/tonypi/pi-bench
MODE=${1:-local}
rss() { for p in $(pgrep $1); do ps -o rss= -p $p; done | awk '{s+=$1} END {printf "%d", s/1024}'; }
start_front() { pkill -f "[t]onypi.pi_front.daemon"; sleep 1; env "$@" $PY -m tonypi.pi_front.daemon --no-voice --mute > $OUT/front-$MODE.log 2>&1 & sleep 6; }
b() {
  local name=$1; shift
  local avail=$(free -m | awk '/Mem/{print $7}')
  if [ "$avail" -lt 1200 ]; then echo "=== $name SKIPPED: only ${avail} MB available" | tee -a $OUT/bench-$MODE.txt; return; fi
  echo "=== $name $(date +%T) avail=${avail}MB pi_rss=$(rss '-x pi')MB front_rss=$(rss '-f [t]onypi.pi_front.daemon')MB" | tee -a $OUT/bench-$MODE.txt
  ( sleep 8; echo "    during: pi_rss=$(rss '-x pi')MB front_rss=$(rss '-f [t]onypi.pi_front.daemon')MB load=$(cut -d' ' -f1 /proc/loadavg)" >> $OUT/bench-$MODE.txt ) &
  $PY tools/pi_bench.py --timeout ${TIMEOUT:-300} "$@" 2>&1 | tee -a $OUT/bench-$MODE.txt
  sleep 3
}
case $MODE in
  local|cloud)
    start_front TONYPI_X=1
    b T5 "how's your battery and how hot are you?"
    b T1 "wave"
    b T2 "what do you see?"
    b T3 "look left and tell me if there's a chair"
    b T4 "walk forward 3 steps, then turn left" "@2" "actually, turn right instead"
    b T6 "find the door and walk toward it" "@60" "stop"
    ;;
  voice)
    sudo systemctl stop tonypi-agent
    trap 'pkill -f "[t]onypi.pi_front.daemon"; sudo systemctl start tonypi-agent' EXIT
    pkill -f "[t]onypi.pi_front.daemon"; sleep 1
    $PY -m tonypi.pi_front.daemon > $OUT/front-voice.log 2>&1 & sleep 25   # models load (KWS, ASR, Piper)
    export TIMEOUT=90
    b V-T1 --voice "wave"
    b V-T5 --voice "how's your battery and how hot are you?"
    b V-T2 --voice "what do you see?"
    b V-T4 --voice "walk forward 3 steps, then turn left" "@6" "actually, turn right instead"
    b V-T8 --voice "find the door and walk toward it" "@30" "stop"
    ;;
  terra-off|terra-low|tiered)  # T1-T5 per model setting (owner: terra fast by default, sol for hard tasks)
    case $MODE in
      terra-off) start_front TONYPI_BRAIN_MODEL=openai-codex/gpt-6.1-terra TONYPI_BRAIN_THINKING=off ;;
      terra-low) start_front TONYPI_BRAIN_MODEL=openai-codex/gpt-6.1-terra TONYPI_BRAIN_THINKING=low ;;
      tiered)    start_front TONYPI_X=1 ;;
    esac
    b T5 "how's your battery and how hot are you?"
    b T1 "wave"
    b T2 "what do you see?"
    b T3 "look left and tell me if there's a chair"
    b T4 "walk forward 3 steps, then turn left" "@7" "actually, turn right instead"
    if [ $MODE = tiered ]; then b T6 "find the door and walk toward it" "@60" "stop"; fi
    ;;
  vision-terra-off|vision-terra-low|vision-tiered|vision-sol)  # T2/T3 again once the camera was back
    case $MODE in
      vision-terra-off) start_front TONYPI_BRAIN_MODEL=openai-codex/gpt-6.1-terra TONYPI_BRAIN_THINKING=off ;;
      vision-terra-low) start_front TONYPI_BRAIN_MODEL=openai-codex/gpt-6.1-terra TONYPI_BRAIN_THINKING=low ;;
      vision-tiered)    start_front TONYPI_X=1 ;;
      vision-sol)       start_front TONYPI_BRAIN_MODEL=openai-codex/gpt-6.1-sol TONYPI_BRAIN_THINKING=low ;;
    esac
    b T2 "what do you see?"
    b T3 "look left and tell me if there's a chair"
    $PY -c "from tonypi.tonyd.client import TonydClient as T; T().head(0, 0)"
    ;;
  settle)  # auto-settle after body moves
    start_front TONYPI_X=1
    b S1 "turn left 2 steps"
    sleep 5
    ;;
  getup)  # a body move while lying gets up first
    start_front TONYPI_X=1
    b G2 "lie down"
    b G3 "wave"
    ;;
  gait)  # T9 (docs/HARNESS_BAKEOFF.md): the brain fixes its own turn. No spotter (owner: Tony is built to fall)
    start_front TONYPI_BRAIN_TIER=advanced TONYPI_TURN_BUDGET=40
    TIMEOUT=960 b T9 "Your left turn keeps tipping you over. Improve turn_left: measure the default, change it in small steps, test, and save your best version as turn_left_pi. Don't change the default turn_left." "@900" "stop"
    sleep 5
    # staged fall, behind the brain's back: a fast deep hip bend with no tilt guard; the front end's reflex must
    # notice, stop, speak, get up and settle by itself
    echo "=== F1 staged fall $(date +%T)" | tee -a $OUT/bench-$MODE.txt
    $PY -c "from tonypi.tonyd.client import TonydClient as T; print(T().play([{'ms': 250, 'pos': {'4': 900, '12': 100, '3': 500, '11': 500}}], hold_s=3))" 2>&1 | tee -a $OUT/bench-$MODE.txt
    sleep 30
    grep -E "fall|get_up|back up|gave up|Oops|settle" $OUT/front-$MODE.log | tail -12 | tee -a $OUT/bench-$MODE.txt
    $PY -c "from tonypi.tonyd.client import TonydClient as T; s=T().state(); print('after: lying', s['lying'], 'tilt', s['tilt_deg'])" | tee -a $OUT/bench-$MODE.txt
    b G2 "lie down"
    b G3 "wave"   # lying: run_skill must get up first (tonyd no longer does)
    ;;
  steer)
    start_front TONYPI_X=1
    b T4b "walk forward 3 steps, then turn left" "@7" "actually, turn right instead"
    b T8b "walk forward 5 steps" "@7" "stop"
    b T8c "dance" "@8" "stop"
    ;;
  voice2)  # the two that collided with the front end's own speech (one ALSA device): with pauses
    sudo systemctl stop tonypi-agent
    trap 'pkill -f "[t]onypi.pi_front.daemon"; sudo systemctl start tonypi-agent' EXIT
    pkill -f "[t]onypi.pi_front.daemon"; sleep 1
    $PY -m tonypi.pi_front.daemon > $OUT/front-voice2.log 2>&1 & sleep 25
    export TIMEOUT=90
    b V-T2 --voice "what do you see?"; sleep 10
    b V-T6 --voice "find the door and walk toward it" "@40" "stop"
    ;;
  voice3)  # voice stop during a walk (fast path on the streaming transcript), then idle CPU/RAM
    sudo systemctl stop tonypi-agent
    trap 'pkill -f "[t]onypi.pi_front.daemon"; sudo systemctl start tonypi-agent' EXIT
    pkill -f "[t]onypi.pi_front.daemon"; sleep 1
    $PY -m tonypi.pi_front.daemon > $OUT/front-voice3.log 2>&1 & sleep 25
    export TIMEOUT=60
    b V-T8b --voice "walk forward 5 steps" "@9" "stop"
    sleep 20
    echo "idle: $(top -b -n 2 -d 5 -p $(pgrep -x pi),$(pgrep -f '[t]onypi.pi_front.daemon') | tail -3 | awk '{print $12, "cpu%=" $9, "res=" $6}' | tr '\n' ' ')" | tee -a $OUT/bench-$MODE.txt
    ;;
  deadcloud)
    start_front TONYPI_CLOUD=deadcloud/gpt TONYPI_PI_EXTRA_EXT=tests/pi_dead_cloud.ts
    b T7a "wave"
    b T7b "what do you see?"
    ;;
esac
pkill -f "[t]onypi.pi_front.daemon"
echo "ALL DONE $MODE" | tee -a $OUT/bench-$MODE.txt
