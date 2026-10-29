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

## Known issues
- llama.cpp issue 31328: LFM3 + thinking off + `response_format` can return HTTP 500 (fix PR 31374 not merged). Not seen
  on our build; `brain.py` falls back to rules if it happens.
- Hash-check downloads (`sha256sum` vs the Hugging Face LFS oid): a corrupted Omni file once produced only "!!!!".

## Not yet tried
- Audio straight into the brain (spoken request → JSON plan in one Omni call), skipping the transcription step.
- LFM3-Omni speech output (Omni can speak; LFM3-Audio's speech was too slow, so probably the same).
- d1-Omni-2B (Liquid's single-pass "decision" model) for fast intent routing.
