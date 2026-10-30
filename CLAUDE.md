# TonyPi — project brain

Hiwonder TonyPi humanoid (16 bus servos + 2 PWM head servos) on a Raspberry Pi 5 8 GB, rebuilt from scratch
on Raspberry Pi OS Lite 64-bit (trixie) with one user (`pi`). This repo replaces all Hiwonder software.
Goal: a self-thinking, self-improving humanoid that hears, sees, controls and learns its own body, upgrades its own
code, uses the owner's iPhone and AI tools, and works on its own goals when nobody talks to it.
**Agents:** Codex (Tony's own brain) reads `AGENTS.md`, not this file: the root `AGENTS.md` is Tony's self-model and
indexes the nested `AGENTS.md` files and `docs/BRAIN.md` (architecture of his mind, runbook). This file is the full
hardware/service reference for Claude Code and for Tony when he needs a detail.

## Hard rules
- **One process owns `/dev/ttyAMA0`: `tonyd`** (`tonypi.hal.board.Board` opens it exclusively; a second open fails with EBUSY). Everything else goes through tonyd's socket or the MCP server. tonyd also owns the camera.
- **Brains author movement** (owner's direction, 2026-10-29): any brain may read joints, play its own keyframes, hold
  poses, save motions and skills (overriding walk/turn/step/...), and calibrate (trim, soft limits; the servos' own
  EEPROM offsets/limits too, but only to fix a bad calibration: `servo_calibrate` needs a reason), all through tonyd
  (`tonyd/studio.py`, MCP tools). The factory motions are defaults and examples. tonyd keeps the reflexes no brain can
  bypass: stop, temperature, torque off when idle.
- `stop` never depends on an LLM. **Falls are the harness's job** (owner's direction, 2026-10-29): tonyd never stops a
  move for tilting, stands Tony up or settles him by itself; it reports `tilt_deg`/`lying` and `max_tilt_deg` per job,
  and a brain can opt in to a tilt stop per job (`guard_tilt_deg`). The brain runs `get_up` / `settle` itself; the
  learning runner gets up after a fall in a trial.
- **Safety posture** (owner's direction, 2026-10-30): Tony lives on the floor. Falling is allowed and expected while
  learning ("it is built to fall on its own and won't be damaged"); recovery is `get_up`, then continue. Trials use
  `guard_tilt_deg` (gaits 40, balance/new poses 30). Never ask the owner about setup, clearance, observers or fixtures:
  verify with sensors (IMU tilt/yaw, joints, head camera at tilt −40, phone). Real limits: stop reflex + latch, servo
  temperature (pause body work at ≥70 °C), battery < 10.9 V = no body work, the irreversible list (reboot, EEPROM
  `servo_calibrate`, deleting user data, auth, network/boot) and safety code (`src/tonypi/tonyd/`, the voice stop
  reflex) need the owner's yes.
- Servo **temperature**: limit is 85 °C (servo self-protects). Measured up to 80 °C after ~1 h of holding a pose on 2026-10-28 (elbows 6/14 hottest at 78–80, right leg 66–76). **Torque is off whenever idle** (any pose, standing included: it carries no load and holds the pose limp); a thermal stop unloads instead of standing.
- **Memory** (8 GB, 2 GB zram, no PSI): the Pi froze once (2026-10-29) with llama-server + several Claude Code sessions
  + harness Node/Hermes jobs. `earlyoom` (`provision/files/earlyoom.default`) kills dev/harness jobs first, then the
  largest process (llama-server, `Restart=always`); tonyd (OOMScoreAdjust −900), voice, agent and MCP (−500) are never chosen.
  Check `free -m` before anything big (second llama-server, builds) and run it under `flock /run/lock/tonypi-robot.lock`.
- **Hang recovery**: the `watchdog` daemon (`provision/files/watchdog.conf`, real-time, owns `/dev/watchdog`; systemd's
  RuntimeWatchdog is off) reboots the Pi if sshd stops answering for 3 min, and the hardware resets it 15 s after the
  daemon itself stalls. `tonypi-bootcheck.service` makes Tony say "System booted from a failure." when the last boot
  didn't end cleanly and saves the end of that boot's log to `/var/log/tonypi/crash-*.log`. Journal is persistent;
  earlyoom logs memory every minute; `psi=1` enables `/proc/pressure`.
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
- Camera: UVC icSpring (`/dev/v4l/by-id/usb-icSpring_icspring_camera-video-index0`, usually `/dev/video0`), YUYV 640×480 only, ≤30 fps;
  intrinsics in `data/camera_calibration.npz`. It dropped off USB once (2026-10-29) until the cable was reseated; tonyd retries every 3 s.
- Audio: USB "PnP Audio Device" (JMTek 0c76:161f) speaker + mic. WonderEcho voice module on CH340 `/dev/ttyUSB0` (optional).
- I2C-1 bus is empty (no sonar/ToF fitted). Pi GPIO25/23 are board keys, GPIO16/26 LEDs.
- Power: EEPROM `PSU_MAX_CURRENT=5000`, `usb_max_current_enable=1` (robot board power).

## Joint map (`src/tonypi/config/robot.yaml`)
L/R: 1/9 ankle roll · 2/10 ankle pitch · 3/11 knee · 4/12 hip pitch · 5/13 hip roll · 6/14 elbow · 7/15 shoulder roll · 8/16 shoulder pitch.
Positions 0–1000, 500 = centre, ≈0.24°/count. Stand pose in robot.yaml. Right shoulder roll (servo 15) is 200 in every stand pose (owner-tuned; `stand`/`stand_slow`
were 160 from the factory files until 2026-10-29, which pulled the right arm out after every turn/walk).

## Motion library
`data/motions/*.json` — 83 keyframe motions converted from the factory `.d6a` files (`{"frames":[{"ms":..,"pos":{"1":..}}]}`).
Key ones: `stand`, `stand_slow`, `stand_up_front`, `stand_up_back`, `go_forward_start` → `go_forward` (loop) → `go_forward_end`,
`back_start/back/back_end`, `turn_left/right(_small_step)`, `left_move/right_move*`, `left_shot/right_shot` (kicks), `wave`, `bow`, `squat`, `sit_ups`, `lie_down`, dances.
Played by `tonypi.motion.library.Player` (one batched frame per keyframe, interruptible).

## Architecture (target)
```
tonyd (hal + 100 Hz state: IMU filter, battery, servo temps, keys)  ← only process on ttyAMA0
  └ control: motion player + editable motion library (studio), thermal/battery watchdog, e-stop
perception: camera loop (OpenCV) + image questions to LFM3-Omni-2B (same llama-server, --mmproj)
audio: mic → VAD → STT → text events; TTS → speaker (barge-in)
mcp: robot MCP server (Streamable HTTP, localhost/LAN) — the stable interface for every brain
brain: live mode (GPT-Live-1 voice + Codex brain, docs/BRAIN.md); offline fallback: LFM3-Omni-2B agent
```

## tonyd (body daemon, `tonypi-tonyd.service`, since M1 2026-10-29)
`src/tonypi/tonyd/` — the only process on `/dev/ttyAMA0` and the camera. **Stop it (`sudo systemctl stop tonypi-tonyd`)
before `tools/bench.py`** or anything else that opens the board.
- Socket `/run/tonypi/tonyd.sock`, JSON lines (`tonypi.tonyd.client.TonydClient`): ops `ping, skills, run, stop, state, head,
  snapshot, relax, subscribe`. `run` takes a skill name from `tonyd/skills.py` (the only list of body skills) + count 1–5;
  one skill at a time, the newest preempts; `wait: true` returns the finished job (`done/failed/cancelled` + `said`).
- Kept here whatever a brain does: `body.py` temperature guard per motion, **torque off 1.5 s after the last job** in
  whatever pose it ended (going limp mid-step can tip Tony over: end in a stable pose, run `settle`, or `hold_s`), 20 s watchdog
  (re-unloads torque left on, warns on overheating / low battery as `warn` events, which the agent speaks).
- `head` takes degrees (pan + = left, ±60; tilt + = up, −40..25). `snapshot` writes `/run/tonypi/snap-NNNNNN.jpg` from the
  latest frame (~30 ms; the camera thread keeps capturing at 15 fps).
  Camera unplugged / back → `warn` event (`kind: "camera"`), which the agent says ("Camera disconnected." /
  "Camera reconnected."; after the current command if busy; at startup if the camera is missing).
- **Tether** (charging cable): the board reports only voltage, so it's a setting (`tether.json` in the user dir, default
  on, `max_steps` 10): walking skills (walk_*, step_left/right, any skill named *walk*) share a step budget since set/reset;
  turns don't count. `tether {on, max_steps, reset}` op / MCP tool. Turn it off when Tony runs on battery.
- Writing moves (`studio.py`): user layer in `~/.local/share/tonypi/` (`motions/*.json` override `data/motions` by name,
  `skills.json` = name → motion sequence with `"*step"` repeated by count, `calibration.json` = per-servo `trim` +
  soft `limits`). Ops `joints, play {frames, hold_s}, pose {positions, ms, hold_s}, torque {ids, on}, motions,
  motion_get/save/delete, skill_save/delete, calibration, servo_calibration, servo_calibrate {id, offset, limits,
  save, reason}` (EEPROM; every call logged to `servo-calibration-log.jsonl`). Jobs take an opt-in `guard_tilt_deg`. Every job reports `max_tilt_deg` and `yaw_deg` (gyro X integral, + = left; matched camera shift within ~1° on
  2026-10-29: default turn_left ≈ 6–13°/step and turn_right ≈ 0–3°/step on the living-room rug), plus `rot_deg` [x, y, z];
  state has `heading_deg` (drifts slowly: use differences).
- Looking around: tonyd `scan {pans, tilt_deg}` (one photo per head angle, each taken after the head settled, head
  returns; ~2.8 s for 3) and `burst {frames, seconds}` (a short "video"); MCP `scan` / `video` return labelled images.
- Every finished job is appended to `~/.local/share/tonypi/jobs.jsonl` (wall-clock times, tilt, yaw, temps, battery);
  op `job {id}` reads one (last 200 in memory); `trace: true` writes an IMU trace to `~/.local/share/tonypi/traces/`.
- MCP: `tonypi-mcp.service` → `http://127.0.0.1:8765/mcp` (Streamable HTTP, no auth, localhost only by default). Tools:
  body (`list_skills, run_skill, wait_motion, get_job, stop, get_state, look`), camera (`snapshot, scan, video`),
  authoring (`get_joints, play_frames, set_pose, set_torque, *_motion, *_skill, calibration, servo_calibrat*,
  tether`), learning (`trial, compare, body_sweep, ledger, body_model, library`; `src/tonypi/learn/`,
  docs/BODY_LEARNING.md), `say`. `.mcp.json` in the repo points Claude Code at it.

## Voice (`tonypi-voice.service`, enabled at boot): Tony's ears + mouth
`src/tonypi/voice/` owns the USB mic + speaker (conflicts with tonypi-agent: starting one stops the other).
GPT-Live-1 (`gpt-live-1-codex`) via `codex app-server` realtime v3 over WebRTC (aiortc) on the shared ChatGPT login
(docs/GPT_LIVE.md); falls back to local wake word + ASR + Piper. Socket `/run/tonypi/voice.sock` (`VoiceClient`): say,
text (typed request), status, tasks, ping, inject (a WAV as if the user spoke; tests). Half duplex by
default (mic silenced while Tony talks: no echo cancellation). "Stop" / "halt" / "freeze" is caught by the voice
service itself and stops tonyd directly with a latch (motion refused until the user speaks again), whatever the brain
does. **Wake word** (2026-10-30): the GPT-Live session sleeps after 30 s idle (`TONYPI_VOICE_IDLE_S`; no delegation,
L turn, running task or Tony speaking; background talk counts for at most 120 s); "Tony" / "hey Tony" (local
sherpa-onnx spotter) starts it again in ~4.5 s and replays the held mic into it (answer heard ~8-10 s after the
wake word). The spotter's "stop/freeze/halt" latches only while the body works. The brain keeps running while the
voice sleeps; if GPT-Live keeps failing, the local ears take requests to the brain and Piper answers. The speaker
(aplay, also when stalled) and mic (arecord) reopen themselves after a USB drop. Details: docs/BRAIN.md.

## Live mode (default, 2026-10-30): GPT-Live-1 + Tony's own Codex brain, full access
Architecture, data files and runbook: **docs/BRAIN.md** (docs/LIVE_MODE_SPEC.md is the older spec; BRAIN.md wins).
`src/tonypi/voice/live.py`: GPT-Live is the voice (prompt `prompts/live_voice.md`: delegate every request); with
`clientManagedHandoffs:false` its Codex app-server thread L (gpt-6.1-terra, effort low, fast tier,
`danger-full-access`, approval never, CODEX_HOME `~/.config/tonypi/codex-live`, config =
provision/files/codex-live.config.toml, keep both identical) is the brain: shell, file edits incl. its own code, live
web search, images, subagents (`.codex/agents/`), skills (`.agents/skills/`), the robot MCP and the phone MCP. L reads
root `AGENTS.md` + `prompts/codex_developer.md` + a memory snapshot; it is persistent (`~/.local/share/tonypi/live/
state.json`), compacts at 80k tokens and rotates after 20 h. Codex speaks its `[FINAL]` messages (bemTags). Tools
`tony_*`: speak now, background tasks (persistent worker threads, tier terra/sol, optional Codex goal; result spoken
once), steer/cancel tasks, restart its own voice. The voice service merges utterance fragments and bridges requests
GPT-Live or Codex missed.
- **Autonomy** (`src/tonypi/voice/autonomy.py`): after ~75 s of quiet a session thread works on the next goal
  (owner goals first, then self goals / the curriculum); owner speech ends it within 2 s. `tony autonomy pause [MIN]`.
- **Memory** (`src/tonypi/brain/`, `~/.local/share/tonypi/brain/`, local git): MEMORY.md, goals.json, journal,
  inbox, curriculum, learning ledger, FTS recall; the `tony` CLI (`tony memory|remember|journal|goal|recall|inbox|say|
  autonomy|history`).
- **Learning**: docs/BODY_LEARNING.md. Acceptance checks: docs/ACCEPTANCE.md.
Logs: `~/.local/share/tonypi/live/conversation.jsonl`, `tasks.jsonl`, the brain journal. Typed: `{"op":"text"}` on
voice.sock. Auth: Hermes stays installed only as the ChatGPT token refresher (`tools/share_chatgpt_login.py`); never
`codex login/logout` in a tonypi CODEX_HOME.

## Fallback: switch to local / live
The bake-off harnesses (own, Hermes, pi) were retired 2026-10-30 to branches `deprecated/harness-{own,hermes,pi}`
(docs/archive/HARNESS_BAKEOFF.md). "Switch to local" (or `tools/switch.sh local`) starts the offline LFM3 agent
(`tonypi-agent`, which stops the voice); "switch to live" (or `tools/switch.sh live`) starts tonypi-voice again.

## Phone (`tonypi-phone.service`, 2026-10-30): Tony's iPhone as a peripheral
Bridge from the iPeripheral repo (deployed from the Mac with its `deploy/install.sh` to `/home/pi/iPeripheral`; protocol in
that repo's `docs/protocol.md`). WebSocket :8766 on the LAN for the phone (token auth, Bonjour), MCP
`http://127.0.0.1:8767/mcp` (in live mode's Codex config as server `phone`, `required = false`), `/live` MJPEG of the phone
camera. Tools: camera, LiDAR depth/obstacles, detection, on-phone LFM3 (`phone_locate`, `phone_look_and_ask`), speaker,
and `phone_mode` mounted (on Tony) / companion (on a stand watching Tony: `phone_track`, `phone_record_jobs`,
`phone_episode_review`, the observer for docs/BODY_LEARNING.md). `phone_say` is disabled for the brain (companion
echo). It never moves Tony: on tonyd.sock it only
subscribes, reads state/skills/joints (≈2 Hz while recording) and sends plain `stop` (no latch); on voice.sock `say`
and, in companion mode only, commands the phone heard as `text` with source `phone_mic` (`phone_app` = typed is
reserved; the bridge doesn't send it yet). live.py drops a `phone_mic` request when Tony's own mic heard the user within
8 s before to 2.5 s after it (same words, GPT-Live already has them), and labels the rest for the brain, also on steers.
Phone speech does not clear the stop latch (only Tony's own mic does). Codex connects to the phone MCP when the voice
starts and doesn't retry: if the bridge was down then, `sudo systemctl restart tonypi-voice` once it is up.

## Agent (offline fallback: `tonypi-agent.service`, since 2026-10-28)
`src/tonypi/agent/` — voice + terminal front end. Owns the mic and speaker; every move goes through tonyd (`body_link.py`).
- Input: terminal (`tonypi-chat` → `/run/tonypi/agent.sock`, JSON lines) and voice ("hey Tony …": sherpa-onnx KWS
  gigaspeech 3.3M → streaming zipformer en-2023-06-26 with hotwords; mic gain at max). Newest command preempts;
  `stop` never reaches the LLM.
- Models: **LFM3 only** (no LFM2.5). One `tonypi-llm.service` = LFM3-Omni-2B Q4_0 + Q4_0 mmproj on :8080 does the brain,
  vision and speech-to-text (~3.1 GB RSS). Evidence and rejected options: `docs/MODELS.md`; rerun `tools/brain_eval.py`.
- `brain.py`: LFM3-Omni via llama-server, `response_format` json_schema with an action **enum** (it can't invent
  moves) + rules (stop, vision, status questions, a named move is never dropped). ~2–4 s per command.
  `chat_template_kwargs.enable_thinking=false` (LFM3 thinks by default: ~50 s). llama-server needs `--swa-full`
  (LFM3 sliding-window layers: else no prompt reuse, ~17 s/command) and `-ub 64 -cms 64` (checkpoint at len-ubatch).
- `sleep` ("go to sleep", "power down"): lie down, check tilt >60°, unload all servos, RGB off; still listens.
  "wake up" or any body move while lying → the agent's BodyLink runs `get_up` first. `/status` shows torque on N/16.
- `tonyd/body.py`: actions → motion sequences; servo temp checked before (≥81 °C skip) and during (≥83 °C stop + torque off)
  every motion; `relax()` (all torque off) at startup and whenever tonyd goes idle, and the 20 s idle watchdog re-unloads
  anything left on; optional per-job tilt stop (`guard_tilt_deg`); idle low-battery warning at 10.5 V.
- `speech.py`: Piper (lessac medium) out; the wake phrase is removed from the transcript by token timestamps
  (ASR writes it as "hey tony", "haytony", "athony", "ain'toni", "eight only"…). After end of speech (0.7 s) the clip
  is re-transcribed by LFM3-Omni (`lfm_transcribe`, ~2 s; 0% vs 18% word errors), streaming text is the fallback
  (`TONYPI_VOICE`: transcribe | direct | stream; `reconcile()` drops LFM3 hallucinations like "Thank you" and keeps a
  move only the draft heard). Timing: after "hey Tony" 3 s to *start* the command, after each spoken answer 6 s to
  start a follow-up without the wake phrase; a command always ends 0.7 s after you stop. The recogniser's own
  silence "endpoints" are ignored until real words arrive. No tones (removed at the user's request).
  `tests/test_listener_flow.py` checks all of this with the real models in simulated time.
- Latency: no conversation history unless the request is a follow-up ("again", "that"…; history costs ~1.7 s, never
  cached); `Brain.plan_stream` hands moves to `_execute` as they stream, run one at a time. Typed: first move ~1.5 s.
  `vision.py`: `describe()` posts a tonyd snapshot to LFM3-Omni on :8080 (~15 s).
- Debug mode ("enable/disable debug mode", spoken or typed; checked before `stop`, off again after a restart): each voice
  command's recording is played back, then the reply, then "Starting X" / "X finished" around each move (no
  streamed early moves). `tests/test_debug_mode.py`.
- Far-field voice is weak with the stock USB mic (works at ~1 m); a mic array would be the real fix.

## Status / next steps
See `docs/ROADMAP.md`. Provisioning: `sudo provision/provision.sh` (idempotent; runs once automatically on first boot via
`tonypi-provision.service`, log in `/var/log/tonypi-provision.log`).

## Dev
- `uv sync` then `uv run pytest`. Python package lives in `src/tonypi`.
- `uv run python tools/bench.py` — read-only hardware check (`sudo systemctl stop tonypi-tonyd` first).
- Use absolute paths in shell commands.
