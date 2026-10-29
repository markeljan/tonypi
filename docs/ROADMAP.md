# Roadmap

Status markers: [x] done · [~] in progress · [ ] todo

## 0. Clean OS (2026-10-28)
- [x] Audit of the old Hiwonder image (users, services, hardware, protocol)
- [x] Motion library converted to JSON (`data/motions`, 83 motions)
- [x] New HAL (`src/tonypi/hal`) tested live: IMU 102 Hz, battery, 16 servos, head PWM
- [x] Reflash to Raspberry Pi OS Lite 64-bit (trixie 2026-10-06) with cloud-init: user `pi`, same SSH host keys, Wi-Fi, this repo in `/home/pi/tonypi`
- [x] First-boot provisioning (`provision/provision.sh`): packages, groups, udev, journald cap, uv, llama.cpp, LFM models, systemd units
  (ran clean 2026-10-28; `tools/bench.py` passes on the new OS, I2C-1 still empty)

## 1. Local models (first priority)
- [x] llama.cpp built natively (NEON/KleidiAI); `llama-bench` + task benchmarks on this Pi → `docs/MODELS.md` (2026-10-29)
- [x] `tonypi-llm.service`: llama-server on 127.0.0.1:8080 with **LFM3-Omni-2B** (brain + vision + speech-to-text), ~2 s per command (2026-10-29; replaced LFM2.5-1.2B)
- [x] Vision: same LFM3-Omni server (no separate VLM service), ~15 s per image
- [x] Speech-to-text second pass with LFM3-Omni: 0% word errors on the robot mic vs 18% streaming-only
- [x] Tested audio straight into the plan (1.3 s faster, less accurate: kept as `TONYPI_VOICE=direct`); end-to-end voice latency 7.3 s → ~3.5 s (2026-10-29)
- [x] Follow-up window: 6 s without the wake phrase after each answer; listening/stopped cues
- [ ] Always-on listening: audio-only addressee detection was 5–8/12 on real speech (docs/MODELS.md); try name-anywhere + d1, or a visual "facing me" cue
- [ ] Same prompts against the Mac's OpenAI-compatible endpoint (LM Studio) for comparison

## 2. Body runtime (`tonyd`)
- [ ] Daemon owning the board: 100 Hz IMU → orientation filter (Mahony), battery + servo temp polling, key events
- [x] Verify torque on/off: 0x0B unload, 0x0C load (2026-10-28); agent `sleep` = lie down + unload, wakes on any move
- [~] Fall detection: tilt guard stops motions (agent); still todo: gyro, protective pose, auto get-up (`get_up` action exists, unverified on hardware)
- [ ] Fall detection (tilt + gyro) → protective pose → classify face-up/face-down → `stand_up_back` / `stand_up_front`
- [ ] Walk state machine (start → loop → end), turn, side-step; head look-at
- [x] Thermal / low-battery watchdog: agent skips hot motions, stops + unloads mid-motion, torque off whenever idle (2026-10-29)
- [~] Local IPC: agent Unix socket `/run/tonypi/agent.sock` (JSON lines, used by `tonypi-chat`)

## 3. Senses
- [ ] Camera service: continuous capture, latest frame + JPEG snapshot, simple detectors (color/AprilTag/person)
- [x] Audio: always-on mic → "hey Tony" wake word → streaming STT → command; Piper TTS → speaker (2026-10-28)
- [ ] Barge-in while speaking; better far-field mic

## 4. Brain
- [ ] Robot MCP server (Streamable HTTP): walk, turn, look, snapshot, play_motion, say, get_status, stop
- [x] Local fast loop: JSON-schema plan (say + ≤6 enum actions), offline, ~2–4 s (`tonypi-agent`, 2026-10-28; LFM3-Omni since 2026-10-29)
- [ ] `escalate(task)` → Claude Opus via Claude Agent SDK (tools=[], robot MCP, PreToolUse gate)
- [ ] "Go get that": VLM target → head tracking → walk-to (visual servoing) loop
- [ ] Optional: Hermes Agent attached to the same MCP server (Telegram / chat)

## 5. Better motion
- [ ] Auto-calibration: pose capture, IMU-refined standing offsets (bounded search, harnessed)
- [ ] MuJoCo model + system ID from logs → learned walking policy (ONNX, 50 Hz)
