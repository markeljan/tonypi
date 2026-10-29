# Models on TonyPi (decided 2026-10-29)

All numbers measured on this Raspberry Pi 5 (8 GB, CPU only), llama.cpp b11971 (74a4fb0), unless marked "Liquid".

## What runs

| Job | Model | Where |
|---|---|---|
| Brain (request → JSON plan of speech + actions) | **LFM3-Omni-2B Q4_0** | `tonypi-llm.service`, :8080 |
| Vision ("what do you see") | same server, **mmproj-LFM3-Omni-2B Q4_0** | :8080 |
| Speech-to-text (second pass on each command) | same server | :8080 |
| Wake word "hey Tony" | sherpa-onnx KWS zipformer gigaspeech 3.3M | in `tonypi-agent` |
| End-of-speech + fallback transcript | sherpa-onnx streaming zipformer en-2023-06-26 | in `tonypi-agent` |
| Voice out | Piper `en_US-lessac-medium` | in `tonypi-agent` |

Installed but not running: **LFM3-1.7B QAD-Q4_0** (text-only backup brain; has a thinking mode).
No LFM2.5 models remain. Liquid has no wake-word or streaming end-of-speech model, so those stay non-Liquid.

## Brain: `tools/brain_eval.py` (30 commands, raw model output, no safety-net rules)

| Model | Correct | Median / max latency |
|---|---|---|
| LFM2.5-1.2B-Instruct Q4_K_M (old) | 26/30 | 3.5 s / 8.6 s |
| LFM2.5-1.2B-Instruct QAD-Q4_0 | 24/30 | 2.7 s / 15 s |
| LFM3-1.7B Q4_K_M, default server flags | 28/30 | 17 s (prompt cache never reused) |
| LFM3-1.7B Q4_K_M, thinking on | 29/30 | 51 s / 103 s |
| LFM3-1.7B Q4_K_M + `--swa-full` | 27/30 | 2.9 s / 5.1 s |
| LFM3-1.7B QAD-Q4_0 + `--swa-full` | 28/30 | 2.7 s / 4.4 s |
| LFM3-Omni-2B Q4_0 + `--swa-full` (test server) | 27/30 | 2.9 s / 5.6 s |
| **LFM3-Omni-2B Q4_0, deployed (`-np 2 -t 3 -tb 4`)** | **27/30** | **2.5 s / 4.8 s** |

- Thinking must be off (`chat_template_kwargs.enable_thinking=false`) for interactive use.
- `--swa-full` is mandatory for LFM3: its sliding-window layers otherwise drop the cached system prompt (`cache_n=1`).
- LFM2.5 also put moves into plain answers (waved while answering "capital of Japan") and said 12×7=48. LFM3 doesn't.
- 30 commands: a 1-point difference is noise. Omni was picked because it also does vision + speech in the same memory.

## Speed of quantisations (llama-bench, LFM3-1.7B, 3 threads)

| Quant | Prompt tok/s | Generate tok/s |
|---|---|---|
| **QAD-Q4_0** | **66** | **10.6** |
| Q4_K_M | 44 | 9.1 |

Q4_0 hits the Arm (KleidiAI) fast path; Liquid's QAD files are trained to keep Q4_0 accurate. 4 threads read prompts faster
(77 tok/s) but generate slower (9.2), hence `-t 3 -tb 4`.

## Vision (2 robot snapshots, persistent server, first-time image)

| Model | Time / image | Notes |
|---|---|---|
| LFM2.5-VL-1.6B (old, reloaded per request: +~20 s) | 17 s | ok |
| LFM2.5-VL-450M | 5 s | fast, misses people |
| LFM3-VL-2B Q4_K_M, no thinking | 16.5 s | caught a seated person Omni missed |
| LFM3-VL-2B, thinking | 83 s | best description, far too slow |
| LFM3-Omni-2B + mmproj Q8_0 | 16 s | richer, missed the seated person |
| **LFM3-Omni-2B + mmproj Q4_0** | **14.5 s** | same quality as Q8_0 |

Liquid's cards: VL-2B (no think) beats Omni by 2–7 points on MMStar / RealWorldQA / EmbSpatial; Omni wins POPE.
Not worth a second ~3 GB server: brain + VL + Audio servers would need ~8 GB.

## Speech-to-text (user's real voice on the robot mic, 36 s of 6 commands)

| Model | Word error rate | Time |
|---|---|---|
| sherpa streaming zipformer (LibriSpeech) | 18% ("confood", "has your battery") | live |
| LFM3-Audio-2B Q4_K_M | 0% | 2.5 s per 3 s command |
| **LFM3-Omni-2B + mmproj Q4_0** | **0%** | **2.1 s per 3 s command** |

Text-to-speech: LFM3-Audio was ~6× slower than real time on the Pi (15 s for a 2.4 s sentence) → Piper stays.

## Memory (RSS)
One Omni server with 2 slots: ~3.1 GB. Separate LFM3 servers were 2.8–3.1 GB each (encoder working buffers).

## Voice pipeline and latency (2026-10-29)

From the end of a spoken command to Tony moving: **~7.3 s → ~3.5 s** (typed commands: first move at ~1.5 s).

| Stage | Before | Now | How |
|---|---|---|---|
| End-of-speech detection | 0.9 s silence | 0.7 s | zipformer endpoint rule 2 |
| Accurate transcript | 2.7 s | ~2 s | LFM3-Omni second pass (kept: see below) |
| Plan → first move | 4.5 s | ~1.4 s | no conversation history in the prompt (it changed every turn, so never cached: +1.7 s); moves start as soon as the streamed plan contains them |
| Speech | after whole reply synthesised | after the first sentence | Piper chunks streamed into aplay |

`tools/voice_eval.py` (30 synthetic "Hey Tony, …" commands + the user's 6 real ones):

| Voice mode (`TONYPI_VOICE`) | Synthetic | Real | Median |
|---|---|---|---|
| **transcribe** (default): Omni transcribes, then plans from text | **26/30** | **6/6** | 4.4–4.8 s |
| direct: audio straight into the plan | 24/30 | 6/6 | 3.2–3.4 s |
| direct, model writes `heard` first | 20/30 | 5/6 | 5.0 s |

`direct` is ~1.3 s faster but picked wrong moves live ("take a bow" → wave): the keyword rules only see the rough
streaming text there. Correct moves beat 1.3 s, so `transcribe` is the default.

Listening timing: after "hey Tony" the command must start within 3 s; after Tony answers a voice command he waits
6 s for a follow-up to start without "hey Tony" (`TONYPI_FOLLOWUP_S`); either way the command ends 0.7 s after you
stop. Rising tone = listening, falling tone = stopped (Tony's own tone is muted from the mic). A lone "hey Tony"
inside the window acts as a wake. Silence/noise never goes to LFM3 (it invents "Thank you"); known hallucinations
are dropped and a move only the streaming draft heard is passed along ("not your head (or: nodd your head)").

## Always listening? (addressee detection, `tools/addressee_eval.py`)

Question: is a speech clip addressed to Tony? 12 real lines by the user, 6 facing Tony and 6 turned to "someone else"
(several share words: "what do you see", dancing, Tony's name).

| Detector | Real voice | Time per clip | Memory |
|---|---|---|---|
| LFM3-Omni-2B, yes/no logprob | 6/12 | 0.7 s | (shared server) |
| d1-omni-600M (`/v1/systemone` noul) | 7/12 | 0.33 s | 1.0 GB |
| d1-omni-600M, choice (robot/person/about/media) | 5/12 | 0.36 s | 1.0 GB |
| d1-Omni-2B Q8_0, noul | **8/12** | 1.2 s | 3.1 GB |

From one microphone, "Can you pass me the remote?" (to a person) and "How's your battery?" (to Tony) sound alike.
d1-Omni-2B mostly detects *direct address by name* (0.86–0.90 for "Wave at us, Tony" / "Tony, look to the left",
0.36 for "I told Tony about the movie") and misses unnamed requests. Synthetic test speech is scored as "media" (TV),
so only real recordings count. Not deployed: wake word + follow-up window instead. The d1 and Silero VAD files were
removed; re-download: `LiquidAI/d1-Omni-2B-GGUF` (Q8_0 + mmproj), `LiquidAI/d1-omni-600M-GGUF` (built on
LFM2.5-Encoder).
Idea worth trying later: spot the bare word "Tony" anywhere (not just "hey Tony …"), then ask d1 whether it's
addressed; or add a visual cue (someone facing the camera).

## Known issues
- llama.cpp issue 31328: LFM3 + thinking off + `response_format` can return HTTP 500 (fix PR 31374 not merged). Not seen
  on our build; `brain.py` falls back to rules if it happens.
- Hash-check downloads (`sha256sum` vs the Hugging Face LFS oid): a corrupted Omni file once produced only "!!!!".

## Not yet tried
- LFM3-Omni speech output (Omni can speak; LFM3-Audio's speech was too slow, so probably the same).
- d1-Omni-2B (Liquid's single-pass "decision" model) for fast intent routing.
