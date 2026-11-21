"""Gemini 3.8 Live (`gemini-3.8-live`) bidirectional voice + vision + non-blocking delegation."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from collections.abc import Awaitable, Callable
from typing import Any, Final

import numpy as np

from tonypi.agent.gemini_brain import (
    GeminiBrain,
    check_action_safety_decision,
    make_beta_client,
)
from tonypi.voice.service import MIC_RATE, OUT_RATE, STOP_WORDS, resample

log = logging.getLogger("tonypi.voice.gemini_live")

DEFAULT_LIVE_MODEL: Final[str] = "gemini-3.8-live"
GEMINI_IN_RATE: Final[int] = MIC_RATE  # 16 kHz PCM16 input
GEMINI_OUT_RATE: Final[int] = 24000  # 24 kHz PCM16 output -> resampled to 48 kHz
DELEGATE_TOOL_NAME: Final[str] = "delegate_to_tony"

GEMINI_LIVE_SYSTEM_PROMPT: Final[str] = (
    "You are Tony, a small friendly tabletop humanoid robot (Hiwonder TonyPi). "
    "Speak naturally and concisely in the first person. "
    "Whenever the user asks you to move, dance, wave, walk, look around, check status, "
    "describe what you see, or perform a physical action, IMMEDIATELY call the non-blocking "
    "tool `delegate_to_tony` with the user's request verbatim. "
    "Never claim a physical move was completed until the tool response confirms it."
)

_RATE_RE: Final[re.Pattern[str]] = re.compile(r"rate=(\d+)", re.IGNORECASE)


def parse_pcm_sample_rate(mime_type: str | None, default: int = GEMINI_OUT_RATE) -> int:
    """Extract PCM sample rate from e.g. ``audio/pcm;rate=24000``."""
    if not mime_type:
        return default
    match = _RATE_RE.search(mime_type)
    if match:
        try:
            rate = int(match.group(1))
            if rate > 0:
                return rate
        except ValueError:
            pass
    return default


def resample_pcm24_to_48(
    pcm_bytes: bytes,
    *,
    src_rate: int = GEMINI_OUT_RATE,
    dst_rate: int = OUT_RATE,
) -> np.ndarray:
    """Convert raw little-endian int16 PCM (default 24 kHz) to 48 kHz int16 samples."""
    if not pcm_bytes:
        return np.zeros(0, dtype=np.int16)
    usable = len(pcm_bytes) - (len(pcm_bytes) % 2)
    if usable <= 0:
        return np.zeros(0, dtype=np.int16)
    samples = np.frombuffer(pcm_bytes[:usable], dtype=np.int16)
    return resample(samples, src_rate, dst_rate)


def build_live_connect_config(
    *,
    system_instruction: str | None = None,
    voice_name: str | None = None,
) -> Any:
    """Build a ``google.genai.types.LiveConnectConfig`` for Gemini 3.8 Live."""
    from google.genai import types

    delegate_decl = types.FunctionDeclaration(
        name=DELEGATE_TOOL_NAME,
        description=(
            "Delegate any physical movement, gesture, dance, status query, or perception "
            "request to TonyPi's brain and body controller without blocking audio."
        ),
        parameters_json_schema={
            "type": "object",
            "properties": {
                "request": {
                    "type": "string",
                    "description": "The user's spoken request verbatim.",
                }
            },
            "required": ["request"],
        },
        behavior=types.Behavior.NON_BLOCKING,
    )

    kwargs: dict[str, Any] = {
        "response_modalities": [types.Modality.AUDIO],
        "system_instruction": system_instruction or GEMINI_LIVE_SYSTEM_PROMPT,
        "input_audio_transcription": types.AudioTranscriptionConfig(),
        "output_audio_transcription": types.AudioTranscriptionConfig(),
        "tools": [types.Tool(function_declarations=[delegate_decl])],
    }
    if voice_name:
        kwargs["speech_config"] = types.SpeechConfig(
            voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=voice_name)
            )
        )
    return types.LiveConnectConfig(**kwargs)


def _get_field(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _default_execute_actions(actions: list[tuple[str, int]], request: str) -> dict[str, Any]:
    """Execute planned actions via local tonyd socket, degrading cleanly if unavailable."""
    executed: list[dict[str, Any]] = []
    if not actions:
        return {"executed": executed, "request": request}
    try:
        from tonypi.tonyd.client import TonydClient

        c = TonydClient()
        try:
            for action, count in actions:
                if action == "stop":
                    c.stop(latch=True)
                    executed.append({"action": "stop", "ok": True})
                    break
                if action == "report_status":
                    executed.append({"action": action, "state": c.state()})
                    continue
                if action == "describe_view":
                    shot = c.snapshot()
                    jpeg_path = shot.get("path") if isinstance(shot, dict) else None
                    desc = None
                    if jpeg_path:
                        from pathlib import Path
                        from tonypi.agent.gemini_brain import describe_image_gemini

                        desc = describe_image_gemini(Path(jpeg_path).read_bytes())
                    executed.append({"action": action, "description": desc or "camera captured"})
                    continue
                res = c.run(action, count)
                executed.append({"action": action, "count": count, "result": res})
        finally:
            c.close()
    except Exception as exc:
        log.warning("tonyd action execution unavailable: %s", exc)
        return {"executed": executed, "error": str(exc), "request": request}
    return {"executed": executed, "request": request}


class GeminiLiveVoice:
    """Bidirectional Gemini 3.8 Live voice + vision session compatible with ``service.Hub``."""

    def __init__(
        self,
        hub: Any,
        *,
        api_key: str | None = None,
        model: str | None = None,
        client: Any | None = None,
        brain: Any | None = None,
        action_executor: Callable[[list[tuple[str, int]], str], Any] | None = None,
        camera_frame_provider: Callable[[], bytes | None] | None = None,
        video_interval_s: float = 1.5,
        on_hard_stop: Callable[[str], None] | None = None,
        telemetry_provider: Callable[[], dict[str, Any]] | None = None,
        safety_gate: bool = False,
        voice_name: str | None = None,
    ) -> None:
        self.hub = hub
        self.audio = getattr(hub, "audio", None)
        self.api_key = api_key
        self.model = model or os.environ.get("TONYPI_GEMINI_LIVE_MODEL", DEFAULT_LIVE_MODEL)
        self.voice_name = voice_name or os.environ.get("TONYPI_GEMINI_LIVE_VOICE") or None
        self._client = client
        self._brain = brain
        self._action_executor = action_executor or _default_execute_actions
        self._camera_frame_provider = camera_frame_provider
        self.video_interval_s = video_interval_s
        self._on_hard_stop = on_hard_stop
        self._telemetry_provider = telemetry_provider
        self._safety_gate = safety_gate

        self.ready = asyncio.Event()
        self.closed = asyncio.Event()
        self.voice_closed = asyncio.Event()
        self.voice_closed.set()

        self.voice_started_t = 0.0
        self.spoke_t = 0.0
        self.last_heard_t = 0.0
        self.voice_gen = 0
        self.last_output_transcript = ""
        self._input_transcript_buf = ""
        self._active_delegations = 0

        self._session_cm: Any = None
        self._session: Any = None
        self._tasks: list[asyncio.Task[Any]] = []
        self._tool_tasks: set[asyncio.Task[Any]] = set()

    def _get_client(self) -> Any:
        if self._client is None:
            self._client = make_beta_client(self.api_key)
        return self._client

    def _get_brain(self) -> Any:
        if self._brain is None:
            self._brain = GeminiBrain(api_key=self.api_key)
        return self._brain

    @property
    def voice_on(self) -> bool:
        return self._session is not None and not self.voice_closed.is_set()

    def busy(self) -> bool:
        return self._active_delegations > 0 or bool(self._tool_tasks)

    def last_activity(self) -> float:
        voice_until = getattr(self.audio, "voice_until", 0.0) if self.audio is not None else 0.0
        return max(self.voice_started_t, voice_until, self.spoke_t)

    def heard_t(self) -> float:
        return max(self.last_heard_t, getattr(self.hub, "last_heard_t", 0.0))

    async def start_brain(self) -> None:
        """Prepare the Gemini client (no external app-server subprocess required)."""
        self.closed.clear()
        self._get_client()

    async def start_voice(self) -> None:
        """Connect a Gemini 3.8 Live WebSocket session and start audio/video/receive loops."""
        if self._session is not None:
            await self.stop_voice()

        self.ready = asyncio.Event()
        self.voice_closed = asyncio.Event()
        self.voice_gen += 1
        self._input_transcript_buf = ""

        config = build_live_connect_config(voice_name=self.voice_name)
        self._session_cm = self._get_client().aio.live.connect(model=self.model, config=config)
        self._session = await self._session_cm.__aenter__()
        self.voice_started_t = time.monotonic()
        self.ready.set()

        self._tasks = [
            asyncio.create_task(self._receive_loop(), name="gemini-live-recv"),
        ]
        if self.audio is not None:
            self._tasks.append(asyncio.create_task(self._mic_loop(), name="gemini-live-mic"))
        if self._camera_frame_provider is not None:
            self._tasks.append(asyncio.create_task(self._video_loop(), name="gemini-live-video"))
        log.info("Gemini Live session connected (%s)", self.model)

    async def send_audio_chunk(self, pcm16: bytes) -> None:
        """Stream a 16 kHz PCM16 mic chunk to Gemini 3.8 Live."""
        if self._session is None or not pcm16:
            return
        from google.genai import types

        await self._session.send_realtime_input(
            audio=types.Blob(data=pcm16, mime_type=f"audio/pcm;rate={GEMINI_IN_RATE}")
        )

    async def send_video_frame(self, jpeg_bytes: bytes) -> None:
        """Stream an optional JPEG camera frame to Gemini 3.8 Live."""
        if self._session is None or not jpeg_bytes:
            return
        from google.genai import types

        await self._session.send_realtime_input(
            video=types.Blob(data=jpeg_bytes, mime_type="image/jpeg")
        )

    async def _mic_loop(self) -> None:
        try:
            while self.voice_on and self.audio is not None:
                chunk = await self.audio.next_chunk()
                self.audio.sent_chunks += 1
                await self.send_audio_chunk(chunk)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("Gemini Live mic loop ended: %s", exc)
            self.voice_closed.set()

    async def _video_loop(self) -> None:
        try:
            while self.voice_on and self._camera_frame_provider is not None:
                jpeg = await asyncio.to_thread(self._camera_frame_provider)
                if jpeg:
                    await self.send_video_frame(jpeg)
                await asyncio.sleep(self.video_interval_s)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("Gemini Live video loop ended: %s", exc)

    async def _receive_loop(self) -> None:
        try:
            while self.voice_on and self._session is not None:
                received_any = False
                async for msg in self._session.receive():
                    received_any = True
                    await self.handle_server_message(msg)
                if not received_any:
                    break
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("Gemini Live receive loop ended: %s", exc)
        finally:
            self.voice_closed.set()

    def _clear_remote_audio(self) -> None:
        if self.audio is not None:
            self.audio.remote = np.zeros(0, dtype=np.int16)
            self.audio.voice_until = 0.0

    def _trigger_hard_stop(self, text: str) -> None:
        """Hard non-LLM stop reflex: immediately halt audio output and body actuators."""
        self._clear_remote_audio()
        if self._on_hard_stop is not None:
            self._on_hard_stop(text)
            return
        if self.hub is not None and hasattr(self.hub, "_stop_body"):
            self.hub._stop_body(text)

    async def handle_server_message(self, msg: Any) -> None:
        """Process a single ``LiveServerMessage`` (audio, transcripts, interruption, tool calls)."""
        server_content = _get_field(msg, "server_content")
        if server_content is not None:
            if _get_field(server_content, "interrupted"):
                self._clear_remote_audio()
                log.info("Gemini Live interrupted: cleared remote audio queue")

            in_tr = _get_field(server_content, "input_transcription")
            if in_tr is not None:
                text = _get_field(in_tr, "text") or ""
                if text:
                    self.last_heard_t = time.monotonic()
                    self._input_transcript_buf += text
                    if STOP_WORDS.match(text) or STOP_WORDS.match(self._input_transcript_buf):
                        self._trigger_hard_stop(self._input_transcript_buf or text)
                    if self.hub is not None and hasattr(self.hub, "on_transcript"):
                        self.hub.on_transcript(text)
                if _get_field(in_tr, "finished"):
                    self._input_transcript_buf = ""

            out_tr = _get_field(server_content, "output_transcription")
            if out_tr is not None:
                out_text = _get_field(out_tr, "text") or ""
                if out_text:
                    self.last_output_transcript = out_text

            model_turn = _get_field(server_content, "model_turn")
            parts = _get_field(model_turn, "parts") if model_turn is not None else None
            if isinstance(parts, list) and self.audio is not None:
                for part in parts:
                    inline = _get_field(part, "inline_data")
                    if inline is None:
                        continue
                    data = _get_field(inline, "data")
                    mime = _get_field(inline, "mime_type") or ""
                    if isinstance(data, (bytes, bytearray)) and data and (
                        not mime or mime.lower().startswith("audio/pcm")
                    ):
                        src_rate = parse_pcm_sample_rate(mime, GEMINI_OUT_RATE)
                        pcm48 = resample_pcm24_to_48(bytes(data), src_rate=src_rate, dst_rate=OUT_RATE)
                        if len(pcm48):
                            self.audio.add_remote(pcm48)

            if _get_field(server_content, "turn_complete"):
                self._input_transcript_buf = ""

        tool_call = _get_field(msg, "tool_call")
        if tool_call is not None:
            function_calls = _get_field(tool_call, "function_calls") or []
            for fc in function_calls:
                task = asyncio.create_task(self._handle_tool_call(fc))
                self._tool_tasks.add(task)
                task.add_done_callback(self._tool_tasks.discard)

    async def _handle_tool_call(self, fc: Any) -> None:
        call_id = _get_field(fc, "id") or ""
        name = _get_field(fc, "name") or DELEGATE_TOOL_NAME
        args = _get_field(fc, "args") or {}
        request = str(args.get("request") or "").strip() if isinstance(args, dict) else ""

        self._active_delegations += 1
        try:
            result = await self.delegate_request(request)
        except Exception as exc:
            log.warning("delegate tool %s failed: %s", name, exc)
            result = {"status": "error", "error": str(exc)}
        finally:
            self._active_delegations = max(0, self._active_delegations - 1)

        if self._session is not None:
            from google.genai import types

            fr_kwargs: dict[str, Any] = {"id": call_id, "name": name, "response": result}
            sched = getattr(types, "FunctionResponseScheduling", None)
            if sched is not None and hasattr(sched, "WHEN_IDLE"):
                fr_kwargs["scheduling"] = sched.WHEN_IDLE
            await self._session.send_tool_response(
                function_responses=[types.FunctionResponse(**fr_kwargs)]
            )

    async def delegate_request(self, request: str) -> dict[str, Any]:
        """Plan & execute a delegated request via ``GeminiBrain`` and body/MCP executor."""
        from tonypi.agent.brain import Plan, postprocess

        text = request.strip()
        if not text:
            return {"status": "ignored", "actions": [], "say": ""}

        if STOP_WORDS.match(text):
            self._trigger_hard_stop(text)
            return {"status": "stopped", "actions": ["stop"], "say": ""}

        if self._safety_gate and self._telemetry_provider is not None:
            telemetry = self._telemetry_provider()
            gate = await asyncio.to_thread(
                check_action_safety_decision,
                text,
                telemetry,
                api_key=self.api_key,
            )
            if not gate.get("is_safe"):
                return {
                    "status": "blocked_unsafe",
                    "actions": [],
                    "say": "That move is not safe right now.",
                    "safety_gate": gate,
                }

        raw_plan = await asyncio.to_thread(self._get_brain().plan, text)
        actions_raw = raw_plan.get("actions") or []
        plan = Plan(
            [(a["action"], int(a.get("count", 1))) for a in actions_raw if isinstance(a, dict) and "action" in a],
            str(raw_plan.get("say") or ""),
            "gemini",
        )
        plan = postprocess(text, plan)

        exec_res: Any = self._action_executor(plan.actions, text)
        if isinstance(exec_res, Awaitable):
            exec_res = await exec_res

        return {
            "status": "ok",
            "actions": [{"action": a, "count": c} for a, c in plan.actions],
            "say": plan.say,
            "execution": exec_res,
        }

    def speak(self, text: str) -> bool:
        """Speak unprompted text through the active Gemini Live session."""
        if not self.voice_on or getattr(self.hub, "voice", "on") != "on":
            return False
        self.spoke_t = time.monotonic()

        async def _send_say() -> None:
            from google.genai import types

            await self._session.send_client_content(
                turns=types.Content(
                    role="user",
                    parts=[types.Part(text=f"Say exactly these words as Tony: {text}")],
                ),
                turn_complete=True,
            )

        try:
            asyncio.get_running_loop().create_task(_send_say())
            return True
        except RuntimeError:
            return False

    def append(self, item_id: str, text: str, speak: bool = True) -> bool:
        _ = item_id
        if not speak:
            return False
        return self.speak(text)

    async def send_text(self, text: str, source: str = "typed") -> dict[str, Any]:
        """Handle a typed or local-ASR request via ``delegate_request``."""
        _ = source
        result = await self.delegate_request(text)
        if result.get("say") and self.voice_on:
            self.speak(result["say"])
        return result

    async def interrupt_all(self, body_only: bool = False) -> None:
        if not body_only:
            self._clear_remote_audio()
        for task in list(self._tool_tasks):
            task.cancel()

    async def stop_voice(self) -> None:
        """Tear down the active Gemini Live WebSocket session cleanly."""
        if self.audio is not None:
            self.audio.drop_hold()
        self.voice_closed.set()
        for task in list(self._tasks):
            task.cancel()
        self._tasks.clear()
        for task in list(self._tool_tasks):
            task.cancel()
        self._tool_tasks.clear()
        cm, self._session_cm, self._session = self._session_cm, None, None
        if cm is not None:
            try:
                await cm.__aexit__(None, None, None)
            except Exception as exc:
                log.warning("closing Gemini Live session: %s", exc)
        self._clear_remote_audio()

    async def close(self) -> None:
        await self.stop_voice()
        self.closed.set()
