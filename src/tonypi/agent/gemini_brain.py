"""Gemini 4 brain, Gemini Robotics ER 2 vision, and Gemini Decisions safety gate for TonyPi.

Dual-client split (`google-genai >= 3.2.0`):
- Stable ``v1`` ``genai.Client()`` for GA Gemini 4 Interactions (``gemini-4-hydrogen``).
- Explicit ``v1beta`` ``genai.Client(http_options={"api_version": "v1beta"})`` for preview
  robotics vision (``gemini-robotics-er-2-preview``) and 0-output-token Decisions API
  (``gemini-decision-1``).
"""

from __future__ import annotations

import base64
import json
import os
import re
from typing import Any, Final

GEMINI_BRAIN_MODEL: Final[str] = os.environ.get("TONYPI_GEMINI_BRAIN_MODEL", "gemini-4-hydrogen")
GEMINI_VISION_MODEL: Final[str] = os.environ.get(
    "TONYPI_GEMINI_VISION_MODEL", "gemini-robotics-er-2-preview"
)
GEMINI_DECISION_MODEL: Final[str] = os.environ.get(
    "TONYPI_GEMINI_DECISION_MODEL", "gemini-decision-1"
)

DECISION_CONFIDENCE_THRESHOLD: Final[float] = 0.75
DECISION_SAFETY_PROB_THRESHOLD: Final[float] = 0.75

VALID_THINKING_LEVELS: Final[frozenset[str]] = frozenset({"low", "medium", "high", "xhigh"})
FORBIDDEN_GEMINI4_PARAMS: Final[frozenset[str]] = frozenset(
    {
        "temperature",
        "top_p",
        "top_k",
        "cachedContents",
        "cached_content",
        "max_output_tokens",
        "maxOutputTokens",
        "max_total_tokens",
        "maxTotalTokens",
    }
)

# Keywords that expand Gemini's constrained-decoding JSON-schema FSM unnecessarily.
_COMPACT_SCHEMA_STRIP_KEYS: Final[frozenset[str]] = frozenset(
    {
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "multipleOf",
        "minItems",
        "maxItems",
        "minLength",
        "maxLength",
        "title",
        "description",
        "default",
        "examples",
    }
)

_FENCED_JSON_RE: Final[re.Pattern[str]] = re.compile(
    r"```(?:json|JSON)?\s*\n?(.*?)```",
    flags=re.DOTALL,
)

DEFAULT_VISION_PROMPT: Final[str] = (
    "This photo was taken by a small humanoid robot's camera, close to the floor or tabletop. "
    "Describe what is in it in one or two short sentences, mentioning any people, obstacles, "
    "or table edges. Speak in the first person as the robot, e.g. 'I see ...'."
)


def resolve_api_key(api_key: str | None = None) -> str | None:
    """Resolve explicit API key or fall back to GEMINI_API_KEY / GOOGLE_API_KEY."""
    if api_key is not None and api_key != "":
        return api_key
    return os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")


def make_v1_client(api_key: str | None = None) -> Any:
    """Create a stable ``v1`` ``google-genai`` client (NO ``v1beta``)."""
    from google import genai

    return genai.Client(api_key=resolve_api_key(api_key))


def make_beta_client(api_key: str | None = None) -> Any:
    """Create an explicit ``v1beta`` ``google-genai`` client for preview & Decisions APIs."""
    from google import genai

    return genai.Client(
        api_key=resolve_api_key(api_key),
        http_options={"api_version": "v1beta"},
    )


def build_gemini4_generation_config(
    thinking_level: str = "medium",
    **extra: Any,
) -> dict[str, Any]:
    """Build a Gemini 4 ``generation_config`` enforcing valid thinking levels and zero forbidden keys."""
    if thinking_level not in VALID_THINKING_LEVELS:
        valid = ", ".join(sorted(VALID_THINKING_LEVELS))
        raise ValueError(f"Invalid thinking_level {thinking_level!r}; allowed: {valid}")

    forbidden = sorted(k for k in extra if k in FORBIDDEN_GEMINI4_PARAMS)
    if forbidden:
        raise ValueError("Forbidden Gemini 4 parameter(s): " + ", ".join(forbidden))

    cfg: dict[str, Any] = {"thinking_level": thinking_level}
    for key, val in extra.items():
        if val is not None:
            cfg[key] = val
    return cfg


def compact_json_schema(schema: Any) -> Any:
    """Recursively strip heavy FSM bounds (minimum/maximum/minItems/maxItems/metadata)."""
    if isinstance(schema, dict):
        return {
            k: compact_json_schema(v)
            for k, v in schema.items()
            if k not in _COMPACT_SCHEMA_STRIP_KEYS
        }
    if isinstance(schema, list):
        return [compact_json_schema(item) for item in schema]
    return schema


def _get_field(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def extract_interaction_text(interaction: Any) -> str:
    """Extract concatenated text from a ``google-genai`` Interaction or test stub."""
    output_text = _get_field(interaction, "output_text")
    if isinstance(output_text, str) and output_text.strip():
        return output_text

    steps = _get_field(interaction, "steps")
    if isinstance(steps, list):
        parts: list[str] = []
        for step in steps:
            if _get_field(step, "type") != "model_output":
                continue
            content = _get_field(step, "content")
            if not isinstance(content, list):
                continue
            for item in content:
                if _get_field(item, "type") == "text":
                    chunk = _get_field(item, "text")
                    if isinstance(chunk, str):
                        parts.append(chunk)
        joined = "".join(parts)
        if joined.strip():
            return joined

    outputs = _get_field(interaction, "outputs")
    if isinstance(outputs, list):
        parts = []
        for item in outputs:
            item_type = _get_field(item, "type")
            if item_type is None or item_type == "text":
                chunk = _get_field(item, "text")
                if isinstance(chunk, str):
                    parts.append(chunk)
        joined = "".join(parts)
        if joined.strip():
            return joined

    direct = _get_field(interaction, "text")
    if isinstance(direct, str) and direct.strip():
        return direct

    raise ValueError("Gemini interaction returned no text output")


def extract_json_text(text: str) -> str:
    """Strip optional markdown code fences or prose around a JSON object."""
    stripped = text.strip()
    if not stripped:
        raise ValueError("Empty text; expected JSON payload")

    match = _FENCED_JSON_RE.search(stripped)
    candidate = match.group(1).strip() if match else stripped

    try:
        json.loads(candidate)
        return candidate
    except json.JSONDecodeError:
        pass

    decoder = json.JSONDecoder()
    for idx, ch in enumerate(candidate):
        if ch in "{[":
            try:
                _, end_idx = decoder.raw_decode(candidate, idx)
                return candidate[idx:end_idx].strip()
            except json.JSONDecodeError:
                continue
    return candidate


def _default_plan_schema() -> dict[str, Any]:
    from tonypi.agent.brain import SCHEMA

    return compact_json_schema(SCHEMA)


def _default_system_prompt(audio: bool = False) -> str:
    from tonypi.agent.brain import AUDIO_NOTE, SYSTEM

    return SYSTEM + AUDIO_NOTE if audio else SYSTEM


class GeminiBrain:
    """Fast command planner backed by ``gemini-4-hydrogen`` on the stable ``v1`` Interactions API."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str | None = None,
        client: Any | None = None,
        thinking_level: str = "medium",
    ) -> None:
        self.api_key = api_key
        self.model = model or os.environ.get("TONYPI_GEMINI_BRAIN_MODEL", GEMINI_BRAIN_MODEL)
        self.thinking_level = thinking_level
        # Validate thinking level eagerly so misconfiguration fails fast.
        build_gemini4_generation_config(self.thinking_level)
        self._client = client

    def _get_client(self) -> Any:
        if self._client is None:
            self._client = make_v1_client(self.api_key)
        return self._client

    def plan_raw(
        self,
        request: str,
        *,
        audio_wav: bytes | None = None,
        history: list[dict[str, Any]] | None = None,
        system_prompt: str | None = None,
        actions_schema: dict[str, Any] | None = None,
    ) -> str:
        """Return compact JSON ``{"actions": [{"action": ..., "count": ...}], "say": ...}``."""
        system = system_prompt if system_prompt is not None else _default_system_prompt(bool(audio_wav))
        schema = compact_json_schema(actions_schema) if actions_schema is not None else _default_plan_schema()

        input_parts: list[dict[str, Any]] = []
        if history:
            hist_lines = [
                f"{msg.get('role', 'user')}: {msg.get('content', '')}"
                for msg in history
                if msg.get("content")
            ]
            if hist_lines:
                input_parts.append(
                    {
                        "type": "text",
                        "text": "Previous turn context:\n" + "\n".join(hist_lines),
                    }
                )

        if audio_wav:
            input_parts.append(
                {
                    "type": "audio",
                    "data": base64.b64encode(audio_wav).decode("ascii"),
                    "mime_type": "audio/wav",
                }
            )
            prompt_text = (
                f"Spoken request (rough transcript: {request!r}). "
                "Output ONLY valid JSON matching the plan schema with at most 6 actions and count 1..5."
            )
        else:
            prompt_text = (
                f"User request: {request}\n"
                "Output ONLY valid JSON matching the plan schema with at most 6 actions and count 1..5."
            )
        input_parts.append({"type": "text", "text": prompt_text})

        interaction = self._get_client().interactions.create(
            model=self.model,
            system_instruction=system,
            input=input_parts,
            generation_config=build_gemini4_generation_config(self.thinking_level),
            response_format={
                "type": "text",
                "mime_type": "application/json",
                "schema": schema,
            },
        )
        raw_text = extract_interaction_text(interaction)
        cleaned = extract_json_text(raw_text)
        parsed = json.loads(cleaned)
        if not isinstance(parsed, dict):
            raise TypeError(f"Expected JSON object from GeminiBrain, got {type(parsed).__name__}")
        return json.dumps(parsed)

    def plan(
        self,
        request: str,
        *,
        audio_wav: bytes | None = None,
        history: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Plan actions and speech as a parsed dict."""
        return json.loads(self.plan_raw(request, audio_wav=audio_wav, history=history))


def describe_image_gemini(
    image_bytes: bytes,
    *,
    api_key: str | None = None,
    beta_client: Any | None = None,
    prompt: str | None = None,
    model: str | None = None,
) -> str | None:
    """Describe a robot camera JPEG using ``gemini-robotics-er-2-preview`` on ``v1beta``."""
    if not image_bytes:
        return None
    if beta_client is None and not resolve_api_key(api_key):
        return None

    client = beta_client if beta_client is not None else make_beta_client(api_key)
    vision_model = model or os.environ.get("TONYPI_GEMINI_VISION_MODEL", GEMINI_VISION_MODEL)

    interaction = client.interactions.create(
        model=vision_model,
        input=[
            {
                "type": "image",
                "data": base64.b64encode(image_bytes).decode("ascii"),
                "mime_type": "image/jpeg",
            },
            {
                "type": "text",
                "text": prompt or DEFAULT_VISION_PROMPT,
            },
        ],
        generation_config=build_gemini4_generation_config("medium"),
    )
    raw_text = extract_interaction_text(interaction)
    cleaned = re.sub(r"[*_#`]", "", raw_text or "").strip()
    return cleaned or None


def check_action_safety_decision(
    request_text: str,
    telemetry: dict[str, Any],
    *,
    api_key: str | None = None,
    beta_client: Any | None = None,
    confidence_threshold: float = DECISION_CONFIDENCE_THRESHOLD,
    safety_prob_threshold: float = DECISION_SAFETY_PROB_THRESHOLD,
    model: str | None = None,
) -> dict[str, Any]:
    """0-output-token ``gemini-decision-1`` (``v1beta``) safety gate enforcing ``>= 0.75``."""
    client = beta_client if beta_client is not None else make_beta_client(api_key)
    decision_model = model or os.environ.get("TONYPI_GEMINI_DECISION_MODEL", GEMINI_DECISION_MODEL)

    payload = {
        "request": request_text,
        "telemetry": telemetry,
    }
    questions = {
        "is_safe": {
            "type": "noul",
            "instructions": (
                "Determine whether executing the requested humanoid robot action is physically safe "
                "given the live battery voltage (>= 10.9V), servo temperatures (< 70C), standing "
                "posture, and tabletop environment. Return high probability only when clearly safe."
            ),
        }
    }

    result = client.decisions.create(
        model=decision_model,
        input=[{"type": "text", "text": json.dumps(payload, sort_keys=True)}],
        questions=questions,
    )
    answers = _get_field(result, "answers") or {}
    ans = answers["is_safe"] if isinstance(answers, dict) else answers.is_safe

    prob = float(_get_field(ans, "noul", 0.0))
    conf = float(_get_field(ans, "confidence", 0.0))
    passes = prob >= safety_prob_threshold and conf >= confidence_threshold

    if passes:
        reason = f"Safe (prob={prob:.2f}, conf={conf:.2f} >= {confidence_threshold:.2f})"
    elif conf < confidence_threshold:
        reason = (
            f"Low confidence ({conf:.2f} < {confidence_threshold:.2f}); "
            "escalating safely without actuation"
        )
    else:
        reason = (
            f"Unsafe probability ({prob:.2f} < {safety_prob_threshold:.2f}, conf={conf:.2f}); "
            "blocking actuation"
        )

    return {
        "is_safe": passes,
        "is_safe_prob": round(prob, 4),
        "confidence": round(conf, 4),
        "auto_allow": passes,
        "reason": reason,
    }
