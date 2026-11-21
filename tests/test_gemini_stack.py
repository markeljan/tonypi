"""Offline unit tests for TonyPi's Gemini 4 + Gemini Robotics ER 2 + Gemini 3.8 Live stack."""

from __future__ import annotations

import asyncio
import base64
import io
import json
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest
from google.genai import interactions as genai_interactions
from google.genai import types as genai_types

from tonypi.agent import brain, gemini_brain, vision
from tonypi.agent.gemini_brain import (
    FORBIDDEN_GEMINI4_PARAMS,
    GEMINI_BRAIN_MODEL,
    GEMINI_DECISION_MODEL,
    GEMINI_VISION_MODEL,
    GeminiBrain,
    build_gemini4_generation_config,
    check_action_safety_decision,
    compact_json_schema,
    describe_image_gemini,
    make_beta_client,
    make_v1_client,
)
from tonypi.voice import service
from tonypi.voice.gemini_live import (
    DELEGATE_TOOL_NAME,
    GeminiLiveVoice,
    build_live_connect_config,
    resample_pcm24_to_48,
)


class _FakeInteractions:
    def __init__(self, responses: list[Any]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return self._responses.pop(0)


class _FakeDecisions:
    def __init__(self, responses: list[Any]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return self._responses.pop(0)


class _FakeClient:
    def __init__(self, *, interaction_responses: list[Any] | None = None, decision_responses: list[Any] | None = None) -> None:
        self.interactions = _FakeInteractions(interaction_responses or [])
        self.decisions = _FakeDecisions(decision_responses or [])


def _assert_no_forbidden_params(call: dict[str, Any]) -> None:
    cfg = call["generation_config"]
    assert cfg["thinking_level"] in {"low", "medium", "high", "xhigh"}
    assert cfg["thinking_level"] != "minimal"
    for bad in FORBIDDEN_GEMINI4_PARAMS:
        assert bad not in cfg
        assert bad not in call


def _collect_keys(node: Any) -> set[str]:
    out: set[str] = set()
    if isinstance(node, dict):
        for k, v in node.items():
            out.add(k)
            out.update(_collect_keys(v))
    elif isinstance(node, list):
        for item in node:
            out.update(_collect_keys(item))
    return out


# ---------------------------------------------------------------------------
# Dual-client & parameter guard tests
# ---------------------------------------------------------------------------


def test_dual_clients_api_version_split() -> None:
    v1 = make_v1_client("dummy-key")
    beta = make_beta_client("dummy-key")
    assert v1._api_client._http_options.api_version == "v1"
    assert beta._api_client._http_options.api_version == "v1beta"


@pytest.mark.parametrize("level", ["low", "medium", "high", "xhigh"])
def test_generation_config_valid_levels(level: str) -> None:
    assert build_gemini4_generation_config(level) == {"thinking_level": level}


def test_generation_config_rejects_minimal_and_forbidden_params() -> None:
    with pytest.raises(ValueError, match="thinking_level"):
        build_gemini4_generation_config("minimal")
    for param in sorted(FORBIDDEN_GEMINI4_PARAMS):
        with pytest.raises(ValueError, match="Forbidden Gemini 4"):
            build_gemini4_generation_config("medium", **{param: 1})


# ---------------------------------------------------------------------------
# GeminiBrain (gemini-4-hydrogen, v1) tests
# ---------------------------------------------------------------------------


def test_gemini_brain_v1_hydrogen_compact_schema_and_audio() -> None:
    fake_v1 = _FakeClient(
        interaction_responses=[
            SimpleNamespace(output_text='```json\n{"actions":[{"action":"wave","count":1}],"say":"Hello!"}\n```'),
            SimpleNamespace(output_text='{"actions":[{"action":"dance","count":1}],"say":"Let us dance!"}'),
        ]
    )
    gb = GeminiBrain(client=fake_v1)

    res1 = gb.plan("wave hello", history=[{"role": "user", "content": "hi"}])
    assert res1 == {"actions": [{"action": "wave", "count": 1}], "say": "Hello!"}

    call0 = fake_v1.interactions.calls[0]
    assert call0["model"] == GEMINI_BRAIN_MODEL == "gemini-4-hydrogen"
    assert call0["generation_config"] == {"thinking_level": "medium"}
    _assert_no_forbidden_params(call0)

    schema = call0["response_format"]["schema"]
    keys = _collect_keys(schema)
    for stripped in ("minimum", "maximum", "minItems", "maxItems", "title", "description"):
        assert stripped not in keys
    assert "wave" in schema["properties"]["actions"]["items"]["properties"]["action"]["enum"]
    genai_interactions.TextResponseFormat.model_validate(call0["response_format"])

    # Audio WAV input
    wav_bytes = b"RIFFfakewavdata"
    res2 = gb.plan("dance", audio_wav=wav_bytes)
    assert res2["actions"] == [{"action": "dance", "count": 1}]
    call1 = fake_v1.interactions.calls[1]
    assert call1["input"][0] == {
        "type": "audio",
        "data": base64.b64encode(wav_bytes).decode("ascii"),
        "mime_type": "audio/wav",
    }
    _assert_no_forbidden_params(call1)


def test_compact_json_schema_strips_bounds_from_brain_schema() -> None:
    compacted = compact_json_schema(brain.SCHEMA)
    keys = _collect_keys(compacted)
    assert not (keys & {"minimum", "maximum", "minItems", "maxItems"})
    assert compacted["required"] == ["actions", "say"]


# ---------------------------------------------------------------------------
# Vision (gemini-robotics-er-2-preview, v1beta) & Decisions (gemini-decision-1, v1beta)
# ---------------------------------------------------------------------------


def test_describe_image_gemini_uses_robotics_er2_v1beta() -> None:
    jpeg = b"\xff\xd8\xff\xe0camera-frame\xff\xd9"
    fake_beta = _FakeClient(
        interaction_responses=[SimpleNamespace(output_text="*I see* a desk and a smiling person.")]
    )

    desc = describe_image_gemini(jpeg, beta_client=fake_beta)
    assert desc == "I see a desk and a smiling person."

    call = fake_beta.interactions.calls[0]
    assert call["model"] == GEMINI_VISION_MODEL == "gemini-robotics-er-2-preview"
    assert call["generation_config"] == {"thinking_level": "medium"}
    _assert_no_forbidden_params(call)
    assert call["input"][0] == {
        "type": "image",
        "data": base64.b64encode(jpeg).decode("ascii"),
        "mime_type": "image/jpeg",
    }

    assert describe_image_gemini(b"", beta_client=fake_beta) is None


def test_check_action_safety_decision_enforces_075_threshold() -> None:
    fake_beta = _FakeClient(
        decision_responses=[
            SimpleNamespace(answers={"is_safe": SimpleNamespace(noul=0.91, confidence=0.84)}),
            SimpleNamespace(answers={"is_safe": SimpleNamespace(noul=0.74, confidence=0.92)}),
            SimpleNamespace(answers={"is_safe": SimpleNamespace(noul=0.95, confidence=0.74)}),
        ]
    )
    telemetry = {"battery_v": 11.6, "max_servo_temp_c": 41.0, "standing": True}

    ok = check_action_safety_decision("wave hello", telemetry, beta_client=fake_beta)
    assert ok["is_safe"] is True
    assert ok["auto_allow"] is True
    assert ok["is_safe_prob"] == 0.91
    assert ok["confidence"] == 0.84

    low_prob = check_action_safety_decision("backflip", telemetry, beta_client=fake_beta)
    assert low_prob["is_safe"] is False
    assert low_prob["auto_allow"] is False

    low_conf = check_action_safety_decision("ambiguous move", telemetry, beta_client=fake_beta)
    assert low_conf["is_safe"] is False
    assert low_conf["auto_allow"] is False

    call0 = fake_beta.decisions.calls[0]
    assert call0["model"] == GEMINI_DECISION_MODEL == "gemini-decision-1"
    assert "none_of_the_above" not in json.dumps(call0["questions"])
    assert call0["questions"]["is_safe"]["type"] == "noul"


# ---------------------------------------------------------------------------
# Brain & vision.describe auto-routing and offline fallback tests
# ---------------------------------------------------------------------------


def _fake_http_json(payload: dict[str, Any]):
    class _Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

    return lambda req, timeout=0: _Resp(json.dumps(payload).encode())


def test_brain_auto_routing_explicit_url_and_offline_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    # 1. Explicit GeminiBrain provider routes through Gemini and fires on_action callback.
    fake_v1 = _FakeClient(
        interaction_responses=[
            SimpleNamespace(output_text='{"actions":[{"action":"wave","count":2}],"say":"Hey there!"}')
        ]
    )
    gb = GeminiBrain(client=fake_v1)
    early_calls: list[tuple[str, int]] = []
    b = brain.Brain(provider="gemini", gemini_brain=gb)
    plan = b.plan_stream("wave twice", on_action=lambda n, c: early_calls.append((n, c)))
    assert plan.actions == [("wave", 2)]
    assert plan.say == "Hey there!"
    assert plan.source == "gemini"
    assert early_calls == [("wave", 2)]

    # 2. Gemini failure falls back cleanly to local llama-server HTTP endpoint.
    class _FailingGemini:
        def plan_raw(self, *a: Any, **kw: Any) -> str:
            raise ConnectionError("network offline")

    local_payload = {"choices": [{"message": {"content": '{"actions":[{"action":"bow","count":1}],"say":"Bow!"}'}}]}
    monkeypatch.setattr(brain.urllib.request, "urlopen", _fake_http_json(local_payload))
    b_fb = brain.Brain(provider="gemini", gemini_brain=_FailingGemini())
    plan_fb = b_fb.plan("bow please")
    assert plan_fb.actions == [("bow", 1)]
    assert plan_fb.say == "Bow!"
    assert plan_fb.source == "llm"

    # 3. Explicit custom url= stays on local path even if GEMINI_API_KEY is set under auto.
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("TONYPI_BRAIN_PROVIDER", "auto")
    b_custom = brain.Brain(url="http://127.0.0.1:9999/v1/chat/completions")
    assert b_custom._use_gemini() is False


def test_vision_describe_auto_routing_and_local_fallback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    img = tmp_path / "snap.jpg"
    img.write_bytes(b"\xff\xd8\xff\xe0snap\xff\xd9")

    # 1. Provider=gemini calls describe_image_gemini.
    monkeypatch.setenv("TONYPI_VISION_PROVIDER", "gemini")
    monkeypatch.setattr(gemini_brain, "describe_image_gemini", lambda data, prompt=None: "I see a friendly judge.")
    assert vision.describe(img, threading.Event()) == "I see a friendly judge."

    # 2. If Gemini raises (offline), falls back cleanly to local MM_URL.
    def _boom(*a: Any, **kw: Any) -> str:
        raise RuntimeError("offline")

    monkeypatch.setattr(gemini_brain, "describe_image_gemini", _boom)
    monkeypatch.setattr(
        vision.urllib.request,
        "urlopen",
        _fake_http_json({"choices": [{"message": {"content": "I see a wooden table."}}]}),
    )
    assert vision.describe(img, threading.Event()) == "I see a wooden table."

    # 3. Provider=local skips Gemini entirely.
    monkeypatch.setenv("TONYPI_VISION_PROVIDER", "local")
    assert vision._should_use_gemini() is False
    assert vision.describe(img, threading.Event()) == "I see a wooden table."


# ---------------------------------------------------------------------------
# Gemini 3.8 Live (`gemini-3.8-live`) & service.py wiring tests
# ---------------------------------------------------------------------------


def test_build_live_connect_config_and_24k_to_48k_resample() -> None:
    cfg = build_live_connect_config()
    assert isinstance(cfg, genai_types.LiveConnectConfig)
    assert cfg.response_modalities == [genai_types.Modality.AUDIO]
    assert cfg.input_audio_transcription is not None
    assert cfg.output_audio_transcription is not None
    decls = cfg.tools[0].function_declarations
    assert len(decls) == 1
    assert decls[0].name == DELEGATE_TOOL_NAME
    assert decls[0].behavior == genai_types.Behavior.NON_BLOCKING

    # 24 kHz -> 48 kHz doubles sample count
    pcm24 = np.linspace(-10000, 10000, 480, dtype=np.int16).tobytes()
    pcm48 = resample_pcm24_to_48(pcm24)
    assert pcm48.dtype == np.int16
    assert len(pcm48) == 960


def test_gemini_live_session_audio_video_stop_interrupt_and_nonblocking_tool() -> None:
    async def run() -> None:
        sent_inputs: list[dict[str, Any]] = []
        tool_responses: list[Any] = []
        connect_calls: list[dict[str, Any]] = []
        msg_queue: asyncio.Queue[Any] = asyncio.Queue()

        class _FakeLiveSession:
            async def send_realtime_input(self, **kwargs: Any) -> None:
                sent_inputs.append(kwargs)

            async def send_tool_response(self, *, function_responses: Any) -> None:
                tool_responses.append(function_responses)

            async def send_client_content(self, **kwargs: Any) -> None:
                sent_inputs.append({"client_content": kwargs})

            async def receive(self):
                while True:
                    item = await msg_queue.get()
                    if item is None:
                        break
                    yield item

        class _FakeConnectCM:
            def __init__(self, model: str, config: Any) -> None:
                connect_calls.append({"model": model, "config": config})
                self.session = _FakeLiveSession()

            async def __aenter__(self):
                return self.session

            async def __aexit__(self, *a: object) -> None:
                return None

        fake_live_client = SimpleNamespace(
            aio=SimpleNamespace(
                live=SimpleNamespace(connect=lambda *, model, config: _FakeConnectCM(model, config))
            )
        )

        # Fake GeminiBrain for non-blocking tool delegation
        fake_v1 = _FakeClient(
            interaction_responses=[
                SimpleNamespace(output_text='{"actions":[{"action":"wave","count":1}],"say":"Waving now!"}')
            ]
        )
        gb = GeminiBrain(client=fake_v1)

        executed: list[tuple[list[tuple[str, int]], str]] = []
        hard_stops: list[str] = []

        audio = service.Audio()
        hub = SimpleNamespace(
            audio=audio,
            voice="on",
            last_heard_t=0.0,
            transcripts=[],
            on_transcript=lambda text: hub.transcripts.append(text),
            _stop_body=lambda text: hard_stops.append(text),
        )

        live_voice = GeminiLiveVoice(
            hub,
            client=fake_live_client,
            brain=gb,
            action_executor=lambda actions, req: executed.append((actions, req)) or {"ok": True},
        )
        await live_voice.start_brain()
        await live_voice.start_voice()

        assert live_voice.voice_on is True
        assert connect_calls[0]["model"] == "gemini-3.8-live"
        assert connect_calls[0]["config"].tools[0].function_declarations[0].behavior == genai_types.Behavior.NON_BLOCKING

        # 1. Stream 16 kHz mic PCM chunk + JPEG video frame
        mic_chunk = (np.full(320, 1200, dtype=np.int16)).tobytes()
        jpeg_frame = b"\xff\xd8\xff\xe0live-cam\xff\xd9"
        await live_voice.send_audio_chunk(mic_chunk)
        await live_voice.send_video_frame(jpeg_frame)
        assert sent_inputs[0]["audio"].mime_type == "audio/pcm;rate=16000"
        assert sent_inputs[0]["audio"].data == mic_chunk
        assert sent_inputs[1]["video"].mime_type == "image/jpeg"
        assert sent_inputs[1]["video"].data == jpeg_frame

        # 2. Receive 24 kHz PCM model audio -> resampled to 48 kHz in audio.remote
        pcm24 = (np.full(240, 4000, dtype=np.int16)).tobytes()
        await msg_queue.put(
            SimpleNamespace(
                server_content=SimpleNamespace(
                    interrupted=False,
                    input_transcription=None,
                    output_transcription=SimpleNamespace(text="Hello!"),
                    turn_complete=False,
                    model_turn=SimpleNamespace(
                        parts=[
                            SimpleNamespace(
                                inline_data=SimpleNamespace(data=pcm24, mime_type="audio/pcm;rate=24000")
                            )
                        ]
                    ),
                ),
                tool_call=None,
            )
        )
        await asyncio.sleep(0.03)
        assert len(audio.remote) == 480  # 240 @ 24kHz -> 480 @ 48kHz
        assert live_voice.last_output_transcript == "Hello!"

        # 3. Interruption clears audio.remote immediately
        await msg_queue.put(
            SimpleNamespace(
                server_content=SimpleNamespace(
                    interrupted=True,
                    input_transcription=None,
                    output_transcription=None,
                    turn_complete=False,
                    model_turn=None,
                ),
                tool_call=None,
            )
        )
        await asyncio.sleep(0.03)
        assert len(audio.remote) == 0

        # 4. Non-blocking tool delegation executes actions via GeminiBrain and returns FunctionResponse
        await msg_queue.put(
            SimpleNamespace(
                server_content=None,
                tool_call=SimpleNamespace(
                    function_calls=[
                        SimpleNamespace(
                            id="call-42",
                            name=DELEGATE_TOOL_NAME,
                            args={"request": "wave hello"},
                        )
                    ]
                ),
            )
        )
        await asyncio.sleep(0.05)
        assert executed == [([("wave", 1)], "wave hello")]
        assert len(tool_responses) == 1
        fr = tool_responses[0][0]
        assert fr.id == "call-42"
        assert fr.name == DELEGATE_TOOL_NAME
        assert fr.response["status"] == "ok"
        assert fr.response["actions"] == [{"action": "wave", "count": 1}]

        # 5. Hard non-LLM STOP_WORDS reflex triggers immediately on input_transcription
        audio.add_remote(np.full(480, 2000, dtype=np.int16))
        await msg_queue.put(
            SimpleNamespace(
                server_content=SimpleNamespace(
                    interrupted=False,
                    input_transcription=SimpleNamespace(text="Hey Tony, stop!", finished=True),
                    output_transcription=None,
                    turn_complete=False,
                    model_turn=None,
                ),
                tool_call=None,
            )
        )
        await asyncio.sleep(0.03)
        assert hard_stops == ["Hey Tony, stop!"]
        assert len(audio.remote) == 0

        await msg_queue.put(None)
        await live_voice.close()
        assert live_voice.closed.is_set()
        assert live_voice.voice_closed.is_set()

    asyncio.run(run())


def test_voice_service_wires_gemini_live_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(service, "VOICE_MODE", "gemini-live")
    assert service._is_live_brain_mode() is True
    assert service._live_session_mode() == "gemini-live"

    hub = service.Hub(live_enabled=False)
    session = hub._create_live_session()
    assert isinstance(session, GeminiLiveVoice)

    async def run() -> None:
        hub.live = session
        hub.mode = "gemini-live"
        st = await hub.handle({"op": "status"}, None)
        assert st["voice_mode"] == "gemini-live"
        assert st["mode"] == "gemini-live"
        assert st["voice"] == session.model

    asyncio.run(run())
