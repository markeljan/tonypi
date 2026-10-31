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
- [x] Follow-up window: 6 s to start a follow-up without the wake phrase after each answer (no tones)
- [ ] Always-on listening: audio-only addressee detection was 5–8/12 on real speech (docs/MODELS.md); try name-anywhere + d1, or a visual "facing me" cue
- [ ] Same prompts against the Mac's OpenAI-compatible endpoint (LM Studio) for comparison

## 2. Body runtime (`tonyd`)
- [x] `tonyd` owns the board + camera; skills API over a Unix socket; torque off when idle; watchdog warnings (M1, 2026-10-29)
- [ ] tonyd: 100 Hz IMU → orientation filter (Mahony), key events
- [x] Verify torque on/off: 0x0B unload, 0x0C load (2026-10-28); agent `sleep` = lie down + unload, wakes on any move
- [~] Fall detection: tilt guard stops motions (agent); still todo: gyro, protective pose, auto get-up (`get_up` action exists, unverified on hardware)
- [ ] Fall detection (tilt + gyro) → protective pose → classify face-up/face-down → `stand_up_back` / `stand_up_front`
- [ ] Walk state machine (start → loop → end), turn, side-step; head look-at
- [x] Thermal / low-battery watchdog: agent skips hot motions, stops + unloads mid-motion, torque off whenever idle (2026-10-29)
- [x] Local IPC: `/run/tonypi/tonyd.sock` (body) + `/run/tonypi/agent.sock` (chat, speech)

## 3. Senses
- [x] Camera service in tonyd: continuous capture, latest frame, ~30 ms JPEG snapshot (M1)
- [ ] Detectors on the live frame (person/YOLO26n, AprilTag)
- [x] Audio: always-on mic → "hey Tony" wake word → streaming STT → command; Piper TTS → speaker (2026-10-28)
- [ ] Barge-in while speaking; better far-field mic

## 4. Brain
- [x] Robot MCP server `tonypi-mcp` (Streamable HTTP :8765); current everyday and learning profiles are documented
  in `docs/MOTION_LAYER.md` (original broad interface replaced).
- [x] Local fast loop: JSON-schema plan (say + ≤6 enum actions), offline, ~2–4 s (`tonypi-agent`, 2026-10-28; LFM3-Omni since 2026-10-29)
- [x] Harness bake-off own / Hermes / pi (2026-10-30, `docs/archive/HARNESS_BAKEOFF.md`): retired to branches
  `deprecated/harness-*`; live mode (GPT-Live-1 voice + Codex brain) is the one harness, LFM3 agent the offline fallback
- [x] Live mode default at boot; root `AGENTS.md` self-model, skills in `.agents/skills/`, robot-specific compaction
  (2026-10-30, `docs/BRAIN.md`)
- [~] Memory, goals, journal, recall (`tony` CLI, `~/.local/share/tonypi/brain`) and the autonomy scheduler: built
  2026-10-30, to be proven by `docs/ACCEPTANCE.md`
- [ ] Voice relay quality: >= 9/10 spoken commands reach the brain exactly once; first motion after "wave" < 8 s
- [ ] "Go get that": phone/camera target → turn by gyro → walk in checked legs (skill walk-to-target), then visual servoing
- [ ] Nightly memory curation session (merge Recent into sections, dedupe, keep MEMORY.md <= 4 KB)

## 5. Better motion
- [~] Library-first motion layer (2026-10-31): ten everyday tools; explicit separate learning profile, provenance,
  calibrated-limit/model verification and guarded composite promotion. See `docs/MOTION_LAYER.md`.
- [ ] Owner-supervised physical acceptance of the imported/verified catalog; model success is not hardware success.
- [~] Body learning (2026-10-30, `docs/BODY_LEARNING.md`): measured `trial` / `compare` / `body_sweep`, job log,
  ledger, body model, skill library, phone as observer; curriculum joint map → weight shift → step → turn → walk 1 m
- [ ] Goals g1-g4: body model of 16 servos, get_up >= 9/10 both sides, walk 1 m straight 3/3, turn 90 deg +-10
- [ ] Optimiser in the runner (ES over gait params between LLM calls) once the hand-driven loop works
- [ ] Auto-calibration: pose capture, IMU-refined standing offsets (bounded search, harnessed)
- [ ] MuJoCo model + system ID from logs → learned walking policy (ONNX, 50 Hz)
