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
  deadcloud)
    start_front TONYPI_CLOUD=deadcloud/gpt TONYPI_PI_EXTRA_EXT=tests/pi_dead_cloud.ts
    b T7a "wave"
    b T7b "what do you see?"
    ;;
esac
pkill -f "[t]onypi.pi_front.daemon"
echo "ALL DONE $MODE" | tee -a $OUT/bench-$MODE.txt
