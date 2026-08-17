"""Anthropic wrapper for the two-stage classifier (SPEC 7.4).

Contract:
- call_cheap(image_path, ocr_text) -> CheapResult
- call_smart(image_path, ocr_text, cheap_result) -> SmartResult

On parse/validation failure we retry EXACTLY once with a "return valid JSON only"
nudge and then raise. We never guess at malformed output (SPEC 7.4).

Never log image bytes, transcribed text, or the API key.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Protocol, runtime_checkable

from pydantic import ValidationError

from ..config import get_settings
from ..logging_setup import get_logger
from ..metrics import record_metric
from .ocr import encode_image
from .prompts import (
    CHEAP_SYSTEM_PROMPT,
    CHEAP_USER_TEMPLATE,
    RETRY_NUDGE,
    SMART_SYSTEM_PROMPT,
    SMART_USER_TEMPLATE,
    VISION_OCR_SYSTEM_PROMPT,
    VISION_OCR_USER_PROMPT,
)
from .schemas import CheapResult, SmartResult

logger = get_logger(__name__)

# Estimated USD per million tokens, for the daily spend metric (SPEC section 10).
# Approximate on purpose - the pilot replaces these with measured numbers.
_PRICING_USD_PER_MTOK: dict[str, tuple[float, float]] = {
    # model_id_prefix: (input, output)
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-sonnet-5": (3.00, 15.00),
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-opus-5": (5.00, 25.00),
    # Gemini (temporary backend - see AI_PROVIDER in .env.example).
    "gemini-flash-lite": (0.10, 0.40),
    "gemini-flash": (0.30, 2.50),
    "gemini-pro": (1.25, 10.00),
}
_DEFAULT_PRICING = (3.00, 15.00)

_MAX_TOKENS_CLASSIFY = 1024
_MAX_TOKENS_OCR = 2048

# ```json ... ``` or ``` ... ``` - stripped defensively before parsing.
_FENCE_RE = re.compile(r"^\s*```(?:json|JSON)?\s*\n?(.*?)\n?\s*```\s*$", re.DOTALL)


class AIClientError(RuntimeError):
    """The model produced unusable output, or the API call failed outright."""


@runtime_checkable
class AIClient(Protocol):
    """Interface both the real and fake clients satisfy."""

    def call_cheap(self, image_path: str, ocr_text: str) -> CheapResult: ...

    def call_smart(
        self, image_path: str, ocr_text: str, cheap_result: CheapResult
    ) -> SmartResult: ...

    def read_text(self, image_path: str) -> str: ...


# --- helpers ----------------------------------------------------------------


def strip_fences(text: str) -> str:
    """Remove markdown code fences the model was told not to emit."""
    match = _FENCE_RE.match(text.strip())
    return match.group(1).strip() if match else text.strip()


def parse_json_object(text: str) -> dict[str, Any]:
    """Parse a JSON object from model text, tolerating fences and stray prose."""
    candidate = strip_fences(text)
    try:
        parsed = json.loads(candidate)
    except ValueError:
        # Last resort: the outermost {...} span. Still validated downstream, so a
        # wrong guess fails the schema rather than becoming a silent bad lead.
        start, end = candidate.find("{"), candidate.rfind("}")
        if start == -1 or end <= start:
            raise AIClientError("model output contained no JSON object") from None
        try:
            parsed = json.loads(candidate[start : end + 1])
        except ValueError as exc:
            raise AIClientError(f"model output was not valid JSON: {exc}") from exc

    if not isinstance(parsed, dict):
        raise AIClientError(f"model output was {type(parsed).__name__}, expected object")
    return parsed


def _estimated_cost_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    for prefix, (in_rate, out_rate) in _PRICING_USD_PER_MTOK.items():
        if model.startswith(prefix):
            break
    else:
        in_rate, out_rate = _DEFAULT_PRICING
    return (input_tokens * in_rate + output_tokens * out_rate) / 1_000_000


# --- real client ------------------------------------------------------------


class AnthropicAIClient:
    """Real Anthropic-backed client."""

    def __init__(self, api_key: str, cheap_model: str, smart_model: str) -> None:
        try:
            import anthropic
        except ImportError as exc:  # pragma: no cover - dependency is pinned
            raise AIClientError("anthropic SDK not installed") from exc

        self._anthropic = anthropic
        self._client = anthropic.Anthropic(api_key=api_key)
        self.cheap_model = cheap_model
        self.smart_model = smart_model

    # -- public API --

    def call_cheap(self, image_path: str, ocr_text: str) -> CheapResult:
        user_text = CHEAP_USER_TEMPLATE.format(ocr_text=ocr_text or "(no text detected)")
        return self._call_validated(
            model=self.cheap_model,
            stage="cheap",
            system=CHEAP_SYSTEM_PROMPT,
            user_text=user_text,
            image_path=image_path,
            schema=CheapResult,
        )

    def call_smart(
        self, image_path: str, ocr_text: str, cheap_result: CheapResult
    ) -> SmartResult:
        user_text = SMART_USER_TEMPLATE.format(
            ocr_text=ocr_text or "(no text detected)",
            cheap_json=cheap_result.model_dump_json(),
        )
        return self._call_validated(
            model=self.smart_model,
            stage="smart",
            system=SMART_SYSTEM_PROMPT,
            user_text=user_text,
            image_path=image_path,
            schema=SmartResult,
        )

    def read_text(self, image_path: str) -> str:
        """Vision OCR through the cheap model (SPEC 7.4 step 1, `vision` backend)."""
        messages = [self._user_message(image_path, VISION_OCR_USER_PROMPT)]
        text = self._create(
            model=self.cheap_model,
            stage="ocr",
            system=VISION_OCR_SYSTEM_PROMPT,
            messages=messages,
            max_tokens=_MAX_TOKENS_OCR,
        )
        return strip_fences(text)

    # -- internals --

    def _user_message(self, image_path: str, text: str) -> dict[str, Any]:
        media_type, data = encode_image(image_path)
        return {
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": media_type,
                        "data": data,
                    },
                },
                {"type": "text", "text": text},
            ],
        }

    def _create(
        self,
        *,
        model: str,
        stage: str,
        system: str,
        messages: list[dict[str, Any]],
        max_tokens: int,
    ) -> str:
        """One Messages API call. Records call count and estimated spend."""
        try:
            response = self._client.messages.create(
                model=model,
                max_tokens=max_tokens,
                system=system,
                messages=messages,
            )
        except self._anthropic.APIError as exc:
            record_metric("ai_call_errors", 1, {"stage": stage, "model": model})
            raise AIClientError(f"{stage} model call failed: {exc}") from exc

        self._record_usage(model, stage, response)

        if getattr(response, "stop_reason", None) == "refusal":
            raise AIClientError(f"{stage} model refused the request")

        return "".join(
            block.text for block in response.content if getattr(block, "type", None) == "text"
        )

    def _record_usage(self, model: str, stage: str, response: Any) -> None:
        labels = {"stage": stage, "model": model}
        record_metric("ai_calls", 1, labels)

        usage = getattr(response, "usage", None)
        input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
        output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
        record_metric("ai_input_tokens", input_tokens, labels)
        record_metric("ai_output_tokens", output_tokens, labels)
        record_metric(
            "ai_estimated_spend_usd",
            _estimated_cost_usd(model, input_tokens, output_tokens),
            labels,
        )

    def _call_validated(
        self,
        *,
        model: str,
        stage: str,
        system: str,
        user_text: str,
        image_path: str,
        schema: type[CheapResult] | type[SmartResult],
    ) -> Any:
        """Call, validate, and on failure retry ONCE with a JSON-only nudge.

        SPEC 7.4: never guess at malformed output - two strikes and we raise so the
        analyzer can mark the story `failed`.
        """
        messages = [self._user_message(image_path, user_text)]

        raw = self._create(
            model=model,
            stage=stage,
            system=system,
            messages=messages,
            max_tokens=_MAX_TOKENS_CLASSIFY,
        )
        try:
            return schema.model_validate(parse_json_object(raw))
        except (AIClientError, ValidationError) as first_error:
            record_metric("ai_parse_retries", 1, {"stage": stage, "model": model})
            logger.warning(
                "ai_output_invalid_retrying", stage=stage, model=model, error=str(first_error)
            )

        # Retry: same request plus the assistant's bad reply and an explicit nudge.
        retry_messages = [
            *messages,
            {"role": "assistant", "content": raw or "(empty)"},
            {"role": "user", "content": RETRY_NUDGE},
        ]
        retry_raw = self._create(
            model=model,
            stage=stage,
            system=system,
            messages=retry_messages,
            max_tokens=_MAX_TOKENS_CLASSIFY,
        )
        try:
            return schema.model_validate(parse_json_object(retry_raw))
        except (AIClientError, ValidationError) as exc:
            record_metric("ai_parse_failures", 1, {"stage": stage, "model": model})
            raise AIClientError(
                f"{stage} model output failed validation twice: {exc}"
            ) from exc


# --- Gemini -----------------------------------------------------------------


class GeminiAIClient:
    """Google Gemini backend, same contract as `AnthropicAIClient`.

    TEMPORARY: added because only a Gemini key is available. The pipeline is
    provider-agnostic behind the `AIClient` protocol, so switching back is a
    config change (`AI_PROVIDER=anthropic`), not a rewrite.

    Uses the REST API through httpx rather than the google SDK - one less
    dependency, and the request shape is small enough to own.
    """

    _BASE = "https://generativelanguage.googleapis.com/v1beta/models"

    def __init__(self, api_key: str, cheap_model: str, smart_model: str) -> None:
        import httpx

        self._httpx = httpx
        self._api_key = api_key
        self.cheap_model = cheap_model
        self.smart_model = smart_model
        self._client = httpx.Client(timeout=60.0)

    # -- public API (identical to AnthropicAIClient) --

    def call_cheap(self, image_path: str, ocr_text: str) -> CheapResult:
        user_text = CHEAP_USER_TEMPLATE.format(ocr_text=ocr_text or "(no text detected)")
        return self._call_validated(
            model=self.cheap_model,
            stage="cheap",
            system=CHEAP_SYSTEM_PROMPT,
            user_text=user_text,
            image_path=image_path,
            schema=CheapResult,
        )

    def call_smart(
        self, image_path: str, ocr_text: str, cheap_result: CheapResult
    ) -> SmartResult:
        user_text = SMART_USER_TEMPLATE.format(
            ocr_text=ocr_text or "(no text detected)",
            cheap_json=cheap_result.model_dump_json(),
        )
        return self._call_validated(
            model=self.smart_model,
            stage="smart",
            system=SMART_SYSTEM_PROMPT,
            user_text=user_text,
            image_path=image_path,
            schema=SmartResult,
        )

    def read_text(self, image_path: str) -> str:
        """Vision OCR through the cheap model (SPEC 7.4 step 1, `vision` backend)."""
        raw = self._generate(
            model=self.cheap_model,
            stage="ocr",
            system=VISION_OCR_SYSTEM_PROMPT,
            turns=[("user", VISION_OCR_USER_PROMPT)],
            image_path=image_path,
            json_mode=False,
        )
        return strip_fences(raw)

    # -- internals --

    def _generate(
        self,
        *,
        model: str,
        stage: str,
        system: str,
        turns: list[tuple[str, str]],
        image_path: str | None,
        json_mode: bool,
    ) -> str:
        """One generateContent call. Records call count and estimated spend.

        The image rides on the first user turn; later turns (the retry nudge) are
        text only, so we do not re-upload the image on a retry.
        """
        contents: list[dict[str, Any]] = []
        for index, (role, text) in enumerate(turns):
            parts: list[dict[str, Any]] = []
            if index == 0 and image_path:
                media_type, data = encode_image(image_path)
                parts.append({"inline_data": {"mime_type": media_type, "data": data}})
            parts.append({"text": text})
            # Gemini names the assistant role "model".
            contents.append({"role": "model" if role == "assistant" else "user", "parts": parts})

        payload: dict[str, Any] = {
            "contents": contents,
            "systemInstruction": {"parts": [{"text": system}]},
            "generationConfig": {
                "temperature": 0,
                "maxOutputTokens": _MAX_TOKENS_CLASSIFY if json_mode else _MAX_TOKENS_OCR,
            },
        }
        if json_mode:
            payload["generationConfig"]["responseMimeType"] = "application/json"

        url = f"{self._BASE}/{model}:generateContent"
        try:
            response = self._client.post(
                url, json=payload, headers={"x-goog-api-key": self._api_key}
            )
            response.raise_for_status()
            body = response.json()
        except self._httpx.HTTPStatusError as exc:
            record_metric("ai_call_errors", 1, {"stage": stage, "model": model})
            detail = _gemini_error_detail(exc.response)
            raise AIClientError(f"{stage} model call failed ({exc.response.status_code}): {detail}") from exc
        except self._httpx.HTTPError as exc:
            record_metric("ai_call_errors", 1, {"stage": stage, "model": model})
            raise AIClientError(f"{stage} model call failed: {exc}") from exc

        self._record_usage(model, stage, body)

        candidates = body.get("candidates") or []
        if not candidates:
            # Safety filters return no candidate at all.
            reason = (body.get("promptFeedback") or {}).get("blockReason", "unknown")
            raise AIClientError(f"{stage} model returned no candidate (reason: {reason})")

        candidate = candidates[0]
        finish = candidate.get("finishReason")
        if finish in ("SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST"):
            raise AIClientError(f"{stage} model blocked the request ({finish})")

        parts = (candidate.get("content") or {}).get("parts") or []
        return "".join(p.get("text", "") for p in parts)

    def _record_usage(self, model: str, stage: str, body: dict[str, Any]) -> None:
        labels = {"stage": stage, "model": model}
        record_metric("ai_calls", 1, labels)

        usage = body.get("usageMetadata") or {}
        input_tokens = int(usage.get("promptTokenCount") or 0)
        output_tokens = int(usage.get("candidatesTokenCount") or 0)
        record_metric("ai_input_tokens", input_tokens, labels)
        record_metric("ai_output_tokens", output_tokens, labels)
        record_metric(
            "ai_estimated_spend_usd",
            _estimated_cost_usd(model, input_tokens, output_tokens),
            labels,
        )

    def _call_validated(
        self,
        *,
        model: str,
        stage: str,
        system: str,
        user_text: str,
        image_path: str,
        schema: type[CheapResult] | type[SmartResult],
    ) -> Any:
        """Call, validate, and on failure retry ONCE with a JSON-only nudge.

        SPEC 7.4: never guess at malformed output - two strikes and we raise so the
        analyzer can mark the story `failed`.
        """
        turns: list[tuple[str, str]] = [("user", user_text)]
        raw = self._generate(
            model=model,
            stage=stage,
            system=system,
            turns=turns,
            image_path=image_path,
            json_mode=True,
        )
        try:
            return schema.model_validate(parse_json_object(raw))
        except (AIClientError, ValidationError) as first_error:
            record_metric("ai_parse_retries", 1, {"stage": stage, "model": model})
            logger.warning(
                "ai_output_invalid_retrying", stage=stage, model=model, error=str(first_error)
            )

        retry_raw = self._generate(
            model=model,
            stage=stage,
            system=system,
            turns=[*turns, ("assistant", raw or "(empty)"), ("user", RETRY_NUDGE)],
            image_path=image_path,
            json_mode=True,
        )
        try:
            return schema.model_validate(parse_json_object(retry_raw))
        except (AIClientError, ValidationError) as exc:
            record_metric("ai_parse_failures", 1, {"stage": stage, "model": model})
            raise AIClientError(
                f"{stage} model output failed validation twice: {exc}"
            ) from exc


def _gemini_error_detail(response: Any) -> str:
    """Pull the human-readable message out of a Gemini error body."""
    try:
        return str((response.json().get("error") or {}).get("message", ""))[:200]
    except Exception:  # noqa: BLE001 - error reporting must never raise
        return "(no detail)"


# --- OpenRouter -------------------------------------------------------------


class OpenRouterAIClient:
    """OpenRouter backend (OpenAI-compatible chat completions API)."""

    _BASE = "https://openrouter.ai/api/v1/chat/completions"

    def __init__(self, api_key: str, cheap_model: str, smart_model: str) -> None:
        import httpx

        self._httpx = httpx
        self._api_key = api_key
        self.cheap_model = cheap_model
        self.smart_model = smart_model
        self._client = httpx.Client(timeout=60.0)

    def call_cheap(self, image_path: str, ocr_text: str) -> CheapResult:
        user_text = CHEAP_USER_TEMPLATE.format(ocr_text=ocr_text or "(no text detected)")
        return self._call_validated(
            model=self.cheap_model,
            stage="cheap",
            system=CHEAP_SYSTEM_PROMPT,
            user_text=user_text,
            image_path=image_path,
            schema=CheapResult,
        )

    def call_smart(
        self, image_path: str, ocr_text: str, cheap_result: CheapResult
    ) -> SmartResult:
        user_text = SMART_USER_TEMPLATE.format(
            ocr_text=ocr_text or "(no text detected)",
            cheap_json=cheap_result.model_dump_json(),
        )
        return self._call_validated(
            model=self.smart_model,
            stage="smart",
            system=SMART_SYSTEM_PROMPT,
            user_text=user_text,
            image_path=image_path,
            schema=SmartResult,
        )

    def read_text(self, image_path: str) -> str:
        messages = [self._user_message(image_path, VISION_OCR_USER_PROMPT)]
        text = self._create(
            model=self.cheap_model,
            stage="ocr",
            system=VISION_OCR_SYSTEM_PROMPT,
            messages=messages,
            max_tokens=_MAX_TOKENS_OCR,
            json_mode=False,
        )
        return strip_fences(text)

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

    def _user_message(self, image_path: str, text: str) -> dict[str, Any]:
        media_type, data = encode_image(image_path)
        return {
            "role": "user",
            "content": [
                {"type": "text", "text": text},
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{media_type};base64,{data}"},
                },
            ],
        }

    def _create(
        self,
        *,
        model: str,
        stage: str,
        system: str,
        messages: list[dict[str, Any]],
        max_tokens: int,
        json_mode: bool,
    ) -> str:
        payload: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "temperature": 0,
            "messages": [{"role": "system", "content": system}, *messages],
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}

        try:
            response = self._client.post(self._BASE, json=payload, headers=self._headers())
            response.raise_for_status()
            body = response.json()
        except self._httpx.HTTPStatusError as exc:
            record_metric("ai_call_errors", 1, {"stage": stage, "model": model})
            detail = _openrouter_error_detail(exc.response)
            raise AIClientError(
                f"{stage} model call failed ({exc.response.status_code}): {detail}"
            ) from exc
        except self._httpx.HTTPError as exc:
            record_metric("ai_call_errors", 1, {"stage": stage, "model": model})
            raise AIClientError(f"{stage} model call failed: {exc}") from exc

        self._record_usage(model, stage, body)

        choices = body.get("choices") or []
        if not choices:
            raise AIClientError(f"{stage} model returned no choices")

        message = choices[0].get("message") or {}
        content = message.get("content") or ""
        if isinstance(content, list):
            content = "".join(
                part.get("text", "") for part in content if part.get("type") == "text"
            )
        return str(content)

    def _record_usage(self, model: str, stage: str, body: dict[str, Any]) -> None:
        labels = {"stage": stage, "model": model}
        record_metric("ai_calls", 1, labels)

        usage = body.get("usage") or {}
        input_tokens = int(usage.get("prompt_tokens") or 0)
        output_tokens = int(usage.get("completion_tokens") or 0)
        record_metric("ai_input_tokens", input_tokens, labels)
        record_metric("ai_output_tokens", output_tokens, labels)
        record_metric(
            "ai_estimated_spend_usd",
            _estimated_cost_usd(model, input_tokens, output_tokens),
            labels,
        )

    def _call_validated(
        self,
        *,
        model: str,
        stage: str,
        system: str,
        user_text: str,
        image_path: str,
        schema: type[CheapResult] | type[SmartResult],
    ) -> Any:
        messages = [self._user_message(image_path, user_text)]

        raw = self._create(
            model=model,
            stage=stage,
            system=system,
            messages=messages,
            max_tokens=_MAX_TOKENS_CLASSIFY,
            json_mode=True,
        )
        try:
            return schema.model_validate(parse_json_object(raw))
        except (AIClientError, ValidationError) as first_error:
            record_metric("ai_parse_retries", 1, {"stage": stage, "model": model})
            logger.warning(
                "ai_output_invalid_retrying", stage=stage, model=model, error=str(first_error)
            )

        retry_messages = [
            *messages,
            {"role": "assistant", "content": raw or "(empty)"},
            {"role": "user", "content": RETRY_NUDGE},
        ]
        retry_raw = self._create(
            model=model,
            stage=stage,
            system=system,
            messages=retry_messages,
            max_tokens=_MAX_TOKENS_CLASSIFY,
            json_mode=True,
        )
        try:
            return schema.model_validate(parse_json_object(retry_raw))
        except (AIClientError, ValidationError) as exc:
            record_metric("ai_parse_failures", 1, {"stage": stage, "model": model})
            raise AIClientError(
                f"{stage} model output failed validation twice: {exc}"
            ) from exc


def _openrouter_error_detail(response: Any) -> str:
    try:
        body = response.json()
        error = body.get("error")
        if isinstance(error, dict):
            return str(error.get("message", ""))[:200]
        return str(error)[:200]
    except Exception:  # noqa: BLE001 - error reporting must never raise
        return "(no detail)"


# --- deterministic fake -----------------------------------------------------


class FakeAIClient:
    """Deterministic client for fixture mode and tests.

    The score is derived from the inputs so tests can force each routing branch
    (SPEC 7.4: 0-4 reject, 5-6 smart model, 7+ skip smart model):

    - OCR text or filename containing `score=N` pins the cheap score to N.
    - Otherwise the score is a stable hash of image path + OCR text.
    """

    _SCORE_RE = re.compile(r"score\s*=\s*(\d{1,2})")

    def __init__(self, cheap_model: str = "fake-cheap", smart_model: str = "fake-smart") -> None:
        self.cheap_model = cheap_model
        self.smart_model = smart_model
        self.cheap_calls = 0
        self.smart_calls = 0
        self.ocr_calls = 0

    @classmethod
    def score_for(cls, image_path: str, ocr_text: str) -> int:
        for source in (ocr_text or "", image_path or ""):
            match = cls._SCORE_RE.search(source)
            if match:
                return max(0, min(10, int(match.group(1))))
        digest = hashlib.sha256(f"{image_path}|{ocr_text}".encode()).digest()
        return digest[0] % 11

    def call_cheap(self, image_path: str, ocr_text: str) -> CheapResult:
        self.cheap_calls += 1
        record_metric("ai_calls", 1, {"stage": "cheap", "model": self.cheap_model})
        record_metric(
            "ai_estimated_spend_usd", 0.0, {"stage": "cheap", "model": self.cheap_model}
        )

        score = self.score_for(image_path, ocr_text)
        return CheapResult(
            score=score,
            explicit_purchase_intent=score >= 7,
            seeking_contractor=score >= 5,
            allowed_category=score >= 3,
            is_spam=score == 0,
            is_offering_services=False,
            asking_for_free=False,
            complaint_only=False,
            service_category="general" if score >= 3 else None,
            geography=None,
            email_visible=None,
        )

    def call_smart(
        self, image_path: str, ocr_text: str, cheap_result: CheapResult
    ) -> SmartResult:
        self.smart_calls += 1
        record_metric("ai_calls", 1, {"stage": "smart", "model": self.smart_model})
        record_metric(
            "ai_estimated_spend_usd", 0.0, {"stage": "smart", "model": self.smart_model}
        )

        # Borderline scores resolve upward on the odd side so both outcomes are
        # reachable from fixtures without touching the network.
        confirmed = cheap_result.score >= 6
        final_score = 7 if confirmed else 4
        return SmartResult(
            confirmed=confirmed,
            final_score=final_score,
            service_category=cheap_result.service_category,
            intent_type="hire" if confirmed else None,
            explanation="Deterministic fixture adjudication.",
        )

    def read_text(self, image_path: str) -> str:
        self.ocr_calls += 1
        return ""


# --- factory ----------------------------------------------------------------

_client: AIClient | None = None


def get_ai_client(force_fake: bool | None = None) -> AIClient:
    """Return the process-wide AI client.

    Fixture mode, or an empty ANTHROPIC_API_KEY, yields the deterministic fake so
    the whole pipeline runs end to end without a real key (SPEC section 4).
    """
    global _client

    settings = get_settings()
    # Fixture mode is about Instagram, not the AI provider: a real key still
    # yields a real client so the classifier can be exercised offline from
    # Instagram. Only an absent key forces the fake.
    use_fake = force_fake if force_fake is not None else not settings.ai_api_key

    if _client is None:
        if use_fake:
            logger.info("ai_client_init", mode="fake")
            _client = FakeAIClient(
                cheap_model=settings.cheap_model, smart_model=settings.smart_model
            )
        else:
            logger.info(
                "ai_client_init",
                mode=settings.ai_provider,
                cheap_model=settings.cheap_model,
                smart_model=settings.smart_model,
            )
            match settings.ai_provider:
                case "gemini":
                    client_cls = GeminiAIClient
                case "openrouter":
                    client_cls = OpenRouterAIClient
                case "anthropic":
                    client_cls = AnthropicAIClient
                case unreachable:
                    raise AIClientError(f"unsupported ai_provider: {unreachable!r}")
            _client = client_cls(
                api_key=settings.ai_api_key,
                cheap_model=settings.cheap_model,
                smart_model=settings.smart_model,
            )
    return _client


def reset_ai_client() -> None:
    """Test hook - drops the cached client so new settings take effect."""
    global _client
    _client = None
