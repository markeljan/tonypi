# GPT-Live-1 integration brief (TonyPi, 2026-10-29)

**[V]** means verified live on this Pi or in the installed codex-cli 0.166.0 source/binary. **[U]** means unverified or inferred.

## 1. Our subscription token today
- **The public `gpt-live-1` route is closed to us.** It uses `wss://api.openai.com/v1/live/sessions` or `POST /v1/live/sessions`. With our ChatGPT/Codex OAuth token the handshake succeeds, but `session.start` returns `forbidden: Voice session access denied.` [V]. This happens for `gpt-live-1` and `gpt-live-1-codex`, with or without `ChatGPT-Account-ID`. The route needs a Platform key (`Authorization: Bearer $OPENAI_API_KEY`), and the Pi has none.
- **The working route is Codex app-server realtime v3, model `gpt-live-1-codex`** [V].
  - Run `codex app-server --listen stdio:// --enable realtime_conversation -c model_provider="openai"` with `OPENAI_API_KEY` and `CODEX_API_KEY` removed from its environment.
  - Codex itself POSTs the SDP offer to `https://chatgpt.com/backend-api/codex/realtime/calls?intent=quicksilver&architecture=avas` and opens the sideband `wss://api.openai.com/v1/live/{rtc_id}`. It adds the bearer, account-id and `OpenAI-Alpha: quicksilver=v2` headers itself.
- **Only WebRTC works.** `transport:{type:"websocket"}` fails at `thread/realtime/start` with "realtime conversation requires API key auth" [V].
- **auth.json must contain a `refresh_token` field.** A placeholder value worked; without the field, Codex sends no auth and gets a 401 [V].
  - The test used a scratch copy of CODEX_HOME, not codex-own itself.
  - This process must never refresh the token: Hermes does that. Single-use refresh tokens are [U], but the rule is a sensible precaution. Re-read auth.json after Hermes refreshes.
- **The route is experimental and undocumented.** Pin codex 0.166.0 and keep a smoke test.

## 2. Session setup
- **Startup order** [V]:
  1. `initialize {capabilities:{experimentalApi:true}}`
  2. `initialized`
  3. `thread/start`
  4. `thread/realtime/start {threadId, outputModality:"audio", version:"v3", transport:{type:"webrtc",sdp}, voice:"cove", prompt, realtimeStartInstructions, clientManagedHandoffs}`

  The RPC returns `{}`. The SDP answer arrives later as the `thread/realtime/sdp` notification.
- **Config is fixed at start.** Our client never sends `session.update`. The persona goes in `prompt` and history in `initialItems` (up to 128 items / 8,192 tokens).
- **Voices** [V, from listVoices]: cove, juniper, maple, spruce, ember, vale, breeze, arbor, sol, aube … viola. `marin` is not available. A wrong voice reportedly gives a 403 [U].
- **No VAD or turn-detection settings.** The model is full duplex and manages turns itself.
- **Audio is Opus over WebRTC**, and aiortc decodes it at 48 kHz [V].
  - Mic: `arecord` on plughw:0,0 at 16 kHz s16 mono, into an aiortc track. aiortc's Opus encoder should resample to 48 kHz itself [U].
  - Speaker: resample the 48 kHz frames to mono and pipe them to `aplay`.
  - aiortc 1.15.0 worked in a scratch venv. It still has to be added to the tonypi venv.
- **One process owns the USB card in both directions.** There is no echo cancellation, so the model may hear Tony's own speech [U, test this]. Session `expires_at` was about 2 h after start.

## 3. Tools, delegation, barge-in
- **GPT-Live has no function tools; it delegates** [V].
- **Delegation round trip** [V, one live run]:
  - The client opens a data channel named `oai-events`.
  - `delegation.created {item:{id, content:[{type:"input_text",text}]}}` arrives and includes the user's request text.
  - The client replies `delegation.context.append {delegation_item_id, content:[…], channel?:"speakable"|"commentary"}` and gets `delegation.context.appended`.
  - The model then spoke the result. A filler started about 0.8 s after end of speech and the answer about 1.4 s after (synthetic espeak audio).
- **Codex splits appends into 500-byte chunks.** Keep results short.
- **Every delegation also runs a turn on the backing Codex thread**, even with `clientManagedHandoffs:true` [V, source]. That spends plan tokens: about 15k input tokens per turn, seen once [U].
  - Mitigation: `realtimeStartInstructions: "Reply skip. Do not use tools."`
  - With `clientManagedHandoffs:false`, the thread's output streams back automatically [V source, not tested live].
- **Don't return results with `appendText` or `appendSpeech`.** Both become `session.context.append`, and plain questions sent that way got no reply.
- **Tests need audio, not text.** Text-only triggering is unreliable, so feed espeak-ng speech into the mic track.
- **Barge-in is native but does not cancel backend work.** The harness must:
  - cancel or serialize motions itself;
  - discard stale results;
  - say something is done only after `get_state` confirms it.
- **The public API uses different names** (for a separate adapter if a key ever arrives):
  - `session.delegation.created` carries no request text; rebuild it from `session.input_transcript.delta`.
  - Replies go through `session.commentary.append` or `session.thinking.append` (≤500 tokens).
  - Voices are marin and others.

## 4. Images
- GPT-Live takes no image input on either route [V]. Flow: `snapshot` → the harness's own vision model → text back to the voice session.
- Optional fallback lane: `gpt-realtime-2.1` on `wss://api.openai.com/v1/realtime?model=gpt-realtime-2.1` with the OAuth bearer [V].
  - Native tools and `input_image` work; a function call took 0.57 s.
  - Never send `OpenAI-Beta`; the server answers `beta_api_shape_disabled`.
  - It is unofficial: billing and terms under OAuth are undocumented.

## 5. Cost and limits
- **Subscription:**
  - No per-minute charge.
  - The voice allowance is undocumented [U].
  - Each delegation also costs Codex plan tokens.
  - `usage_limit` was null, so meter locally from `session.usage.updated.audio_duration_ms`.
- **Public API:**
  - $0.05/min, billed per second; WebRTC creation bills 15 s up front; backend usage is billed separately.
  - Concurrent sessions: 50 / 300 / 500 by tier.
  - Instructions ≤16,384 tokens; 128k context with automatic compaction.

## 6. Reference snippet (subscription route; config is passed at start, not via `session.update`)
```python
import asyncio, json, os, fractions, av
from aiortc import RTCPeerConnection, RTCSessionDescription, MediaStreamTrack
P = asyncio.subprocess.PIPE

class Mic(MediaStreamTrack):
    kind = "audio"
    def __init__(s, proc): super().__init__(); s.p, s.pts = proc, 0
    async def recv(s):
        buf = await s.p.stdout.readexactly(640)            # 20 ms @ 16 kHz s16 mono
        f = av.AudioFrame(format="s16", layout="mono", samples=320)
        f.planes[0].update(buf); f.sample_rate = 16000
        f.pts, f.time_base = s.pts, fractions.Fraction(1, 16000); s.pts += 320
        return f

async def main(route_to_harness):
    env = {k: v for k, v in os.environ.items() if k not in ("OPENAI_API_KEY", "CODEX_API_KEY")}
    env["CODEX_HOME"] = "/home/pi/.config/tonypi/codex-own"
    cx = await asyncio.create_subprocess_exec("codex", "app-server", "--listen", "stdio://",
        "--enable", "realtime_conversation", "-c", 'model_provider="openai"',
        stdin=P, stdout=P, env=env, limit=1 << 24)
    pend, sdp_q, n = {}, asyncio.Queue(), 0
    async def reader():
        async for line in cx.stdout:
            m = json.loads(line)
            if "id" in m and m["id"] in pend: pend.pop(m["id"]).set_result(m.get("result"))
            elif m.get("method") == "thread/realtime/sdp": await sdp_q.put(m["params"]["sdp"])
    asyncio.create_task(reader())
    async def rpc(method, params=None):
        nonlocal n; n += 1; fut = pend[n] = asyncio.get_running_loop().create_future()
        cx.stdin.write((json.dumps({"id": n, "method": method, "params": params or {}}) + "\n").encode())
        return await fut
    await rpc("initialize", {"clientInfo": {"name": "tonypi-voice", "version": "0"},
                             "capabilities": {"experimentalApi": True}})
    cx.stdin.write(b'{"method":"initialized"}\n')
    tid = (await rpc("thread/start"))["thread"]["id"]       # result shape [U]

    mic = await asyncio.create_subprocess_exec("arecord", "-D", "plughw:0,0", "-f", "S16_LE",
        "-r", "16000", "-c", "1", "-t", "raw", stdout=P)
    spk = await asyncio.create_subprocess_exec("aplay", "-D", "plughw:0,0", "-f", "S16_LE",
        "-r", "48000", "-c", "1", "-t", "raw", stdin=P)
    pc = RTCPeerConnection(); pc.addTrack(Mic(mic)); dc = pc.createDataChannel("oai-events")
    rs = av.AudioResampler(format="s16", layout="mono", rate=48000)

    @pc.on("track")
    def on_track(t):
        async def play():
            while True:
                for f in rs.resample(await t.recv()): spk.stdin.write(f.to_ndarray().tobytes())
        asyncio.create_task(play())

    @dc.on("message")
    def on_msg(raw):
        ev = json.loads(raw)
        if ev.get("type") == "delegation.created":
            it = ev["item"]; text = " ".join(c["text"] for c in it["content"])
            async def run():
                result = await route_to_harness(text)       # robot tools happen here
                dc.send(json.dumps({"type": "delegation.context.append",
                    "delegation_item_id": it["id"],
                    "content": [{"type": "input_text", "text": result[:450]}]}))
            asyncio.create_task(run())

    await pc.setLocalDescription(await pc.createOffer())
    await rpc("thread/realtime/start", {"threadId": tid, "outputModality": "audio", "version": "v3",
        "transport": {"type": "webrtc", "sdp": pc.localDescription.sdp}, "voice": "cove",
        "prompt": "You are Tony's voice. Delegate every robot action or question.",
        "realtimeStartInstructions": "Reply skip. Do not use tools.", "clientManagedHandoffs": True})
    await pc.setRemoteDescription(RTCSessionDescription(await sdp_q.get(), "answer"))
    await asyncio.Event().wait()
```

## 7. Recommended architecture
> 2026-10-30: decided. Live mode (GPT-Live + its own Codex brain, `clientManagedHandoffs:false`) is the only harness; the Hermes/pi relay below is history. See `docs/BRAIN.md`.

**Shared piece:** a single `tonypi-voice` peripheral service. It owns the USB audio and runs app-server v3 plus aiortc. It sends each delegation's text to the active harness and returns the reply text. Robot motions stay serialized under the robot lock.

- **Own Codex harness:** start realtime on the harness's own app-server thread, which already has the robot tools, with `clientManagedHandoffs:false`. That thread then does the work and avoids the extra skip turn. This setup is not yet tested live; the client-managed path is the fallback.
- **Hermes:** its built-in `gpt-live` mode works only with an API key. Use the peripheral with `clientManagedHandoffs:true` and the "skip" instructions. Forward the delegation text to Hermes and append its final answer.
- **pi:** it has no voice feature. Use the same client-managed peripheral, routing the text to the pi session. pi's `client.live` SDK needs an API key.
