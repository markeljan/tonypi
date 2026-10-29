# TonyPi — project brain

Hiwonder TonyPi humanoid (16 bus servos + 2 PWM head servos) on a Raspberry Pi 5 8 GB, rebuilt from scratch
on Raspberry Pi OS Lite 64-bit (trixie) with one user (`pi`). This repo replaces all Hiwonder software.
Goal: an always-on humanoid that hears, sees, knows its orientation, gets up when knocked over, and does what
it's told ("hey, go get that"), with local LFM models on the Pi and Claude (Opus) for harder reasoning.

## Hard rules
- **One process owns `/dev/ttyAMA0`** (`tonypi.hal.board.Board`). Everything else goes through that process (IPC/MCP). Never open the port twice.
- Motion only through the supervisor/motion layer. LLMs call skill-level tools (walk, turn, look, play_motion, say, stop), never raw joint writes.
- `stop` and fall recovery never depend on an LLM.
- Before commanding motion, assume the robot may be standing on a table: start slow, prefer `stand_slow`, keep a hand near it the first time.
- Servo **temperature**: limit is 85 °C (servo self-protects). Measured up to 80 °C after ~1 h of holding a pose on 2026-10-28 (elbows 6/14 hottest at 78–80, right leg 66–76). Unload or sit/lie the robot when idle; add a thermal watchdog.
- No `sudo chmod 777` hacks. Access is via groups (dialout, gpio, i2c, video, audio) + `provision/files/99-tonypi.rules`.

## Hardware facts (verified live 2026-10-28 with `tools/bench.py`)
- Controller: STM32 board on `/dev/ttyAMA0` @ 1,000,000 8N1 (needs `dtparam=uart0=on`; no serial console).
- Frame: `AA 55 FUNC LEN DATA CRC8` — CRC-8/MAXIM over FUNC,LEN,DATA. FUNC: 0 SYS, 1 LED, 2 BUZZER, 4 PWM servo, 5 bus servo, 6 KEY, 7 IMU, 11 RGB. See `src/tonypi/hal/proto.py`.
- IMU: streamed automatically at **~102 Hz** (dt p50 9.8 ms), `<6f` = accel in **g** (|a|≈0.975), gyro probably **deg/s** (bias ≈ -1.05, 0.14, -0.37).
  **Upright standing ⇒ gravity ≈ -X** (ax ≈ -0.96 g). Old fall logic: `atan2(ax, az)`; ≈0° face-down, ≈180° on back.
- Battery: board pushes SYS frames (~10 Hz), 11.56 V measured (3S LiPo, ~12.6 V full; set low-voltage stop ≈ 10.5 V, verify).
- Bus servos 1–16 respond; 17/18 absent (no grippers). Per-servo read ≈ 2.8 ms; **all 16 positions in 33 ms (~30 Hz)**.
  EEPROM offsets match `config/robot.yaml` deviations. Angle limits 0–1000 on all. torque_state=1 when holding.
- Torque: **0x0B = unload (off), 0x0C = load (on)**, READ 0x0D reports 0/1 (verified on servo 7, 2026-10-28). Any MOVE re-enables torque. `Board.torque(ids, on)`.
- Lying orientation: on its back ⇒ az ≈ -0.97 g (`stand_up_back`), verified 2026-10-28; face-down assumed az > 0.
- Lights: RGB pixels via FUNC 11 can be set off; a red and a blue LED stay lit (power/status, not switchable).
- Buzzer: `AA 55 02` `<HHHH` freq, on_ms, off_ms, repeat. **off_ms=0 = stays on forever**; `Board.beep` clamps it, `Board.buzzer_off()` sends all zeros.
- Head PWM: ch1 tilt (1500 centre, 1081 look-down), ch2 pan (1466 centre). Readback works.
- Camera: UVC `/dev/video0`, YUYV 640×480 only, ≤30 fps; intrinsics in `data/camera_calibration.npz`.
- Audio: USB "PnP Audio Device" (JMTek 0c76:161f) speaker + mic. WonderEcho voice module on CH340 `/dev/ttyUSB0` (optional).
- I2C-1 bus is empty (no sonar/ToF fitted). Pi GPIO25/23 are board keys, GPIO16/26 LEDs.
- Power: EEPROM `PSU_MAX_CURRENT=5000`, `usb_max_current_enable=1` (robot board power).

## Joint map (`src/tonypi/config/robot.yaml`)
L/R: 1/9 ankle roll · 2/10 ankle pitch · 3/11 knee · 4/12 hip pitch · 5/13 hip roll · 6/14 elbow · 7/15 shoulder roll · 8/16 shoulder pitch.
Positions 0–1000, 500 = centre, ≈0.24°/count. Stand pose in robot.yaml (servo 15: 200 there vs 160 in `stand.json` — pick one on the robot).

## Motion library
`data/motions/*.json` — 83 keyframe motions converted from the factory `.d6a` files (`{"frames":[{"ms":..,"pos":{"1":..}}]}`).
Key ones: `stand`, `stand_slow`, `stand_up_front`, `stand_up_back`, `go_forward_start` → `go_forward` (loop) → `go_forward_end`,
`back_start/back/back_end`, `turn_left/right(_small_step)`, `left_move/right_move*`, `left_shot/right_shot` (kicks), `wave`, `bow`, `squat`, `sit_ups`, `lie_down`, dances.
Played by `tonypi.motion.library.Player` (one batched frame per keyframe, interruptible).

## Architecture (target)
```
tonyd (hal + 100 Hz state: IMU filter, battery, servo temps, keys)  ← only process on ttyAMA0
  └ control: motion player, walk state machine, fall detect → get-up, thermal/battery watchdog, e-stop
perception: camera loop (OpenCV) + on-demand VLM (LFM2.5-VL via llama-server --mmproj)
audio: mic → VAD → STT → text events; TTS → speaker (barge-in)
mcp: robot MCP server (Streamable HTTP, localhost/LAN) — the stable interface for every brain
brain: thin local loop (LFM2.5-1.2B, few tools, fast, offline) + escalate() to Claude Opus via Claude Agent SDK
```
Hermes Agent (Nous) can attach to the same MCP server later (it needs ≥64K context, so not for the local 1.2B tier).

## Agent (live: `tonypi-agent.service`, since 2026-10-28)
`src/tonypi/agent/` — the always-on process. **It owns `/dev/ttyAMA0`**: `sudo systemctl stop tonypi-agent` before
`tools/bench.py` or any script that opens the board, and start it again after.
- Input: terminal (`tonypi-chat` → `/run/tonypi/agent.sock`, JSON lines) and voice ("hey Tony …": sherpa-onnx KWS
  gigaspeech 3.3M → streaming zipformer en-2023-06-26 with hotwords; mic gain at max). Newest command preempts;
  `stop` never reaches the LLM.
- `brain.py`: LFM2.5-1.2B via llama-server, `response_format` json_schema with an action **enum** (it can't invent
  moves) + rules (stop, vision, status questions, a named move is never dropped). ~2–4 s per command.
  llama-server needs `-ub 64 -cms 64`: LFM2 is hybrid, prompt cache resumes only from a checkpoint at len-ubatch.
- `sleep` ("go to sleep", "power down"): lie down, check tilt >60°, unload all servos, RGB off; still listens.
  "wake up" or any body move while lying → `stand_up_back`/`front` → stand. `/status` shows torque on N/16.
- `body.py`: actions → motion sequences; servo temp checked before (≥81 °C skip) and during (≥83 °C stop + stand)
  every motion; fall guard tilt >70° for 0.3 s (dances lean past 50° on purpose); idle warning at 80 °C / 10.5 V.
- `speech.py`: Piper (lessac medium) out; the wake phrase is removed from the transcript by token timestamps
  (ASR writes it as "hey tony", "haytony", "athony", "eight only"…). `vision.py`: snapshot + LFM2.5-VL-1.6B (~25 s).
- Far-field voice is weak with the stock USB mic (works at ~1 m); a mic array would be the real fix.

## Status / next steps
See `docs/ROADMAP.md`. Provisioning: `sudo provision/provision.sh` (idempotent; runs once automatically on first boot via
`tonypi-provision.service`, log in `/var/log/tonypi-provision.log`).

## Dev
- `uv sync` then `uv run pytest`. Python package lives in `src/tonypi`.
- `uv run python tools/bench.py` — read-only hardware check (`sudo systemctl stop tonypi-agent` first).
- Use absolute paths in shell commands.
