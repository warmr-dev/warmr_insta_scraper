"""Pydantic schemas validating model output.

Field names are normative per SPEC 7.4 - the cheap prompt lists exactly these keys.
Both models are strict: unknown fields and out-of-range scores are rejected so a
malformed response fails loudly instead of being silently coerced.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, Field, field_validator

# A "sentence" ends at . ! or ? (optionally followed by quotes/brackets).
_SENTENCE_END = re.compile(r"[.!?]+[\"')\]]*\s+|[.!?]+[\"')\]]*$")

MAX_EXPLANATION_SENTENCES = 2
MAX_EXPLANATION_CHARS = 400


class CheapResult(BaseModel):
    """Stage-1 classifier output (SPEC 7.4 step 2)."""

    model_config = ConfigDict(extra="forbid", strict=False)

    score: int = Field(ge=0, le=10)
    explicit_purchase_intent: bool
    seeking_contractor: bool
    allowed_category: bool
    is_spam: bool
    is_offering_services: bool
    asking_for_free: bool
    complaint_only: bool
    service_category: str | None = None
    geography: str | None = None
    email_visible: str | None = None

    @field_validator("service_category", "geography", "email_visible", mode="before")
    @classmethod
    def _blank_to_none(cls, value: object) -> object:
        """Models like to emit "" or "null" instead of JSON null."""
        if isinstance(value, str):
            stripped = value.strip()
            if stripped == "" or stripped.lower() in {"null", "none", "n/a"}:
                return None
            return stripped
        return value


class SmartResult(BaseModel):
    """Stage-2 adjudicator output for borderline scores (SPEC 7.4 step 3)."""

    model_config = ConfigDict(extra="forbid", strict=False)

    confirmed: bool
    final_score: int = Field(ge=0, le=10)
    service_category: str | None = None
    intent_type: str | None = None
    explanation: str = Field(min_length=1, max_length=MAX_EXPLANATION_CHARS)

    @field_validator("service_category", "intent_type", mode="before")
    @classmethod
    def _blank_to_none(cls, value: object) -> object:
        if isinstance(value, str):
            stripped = value.strip()
            if stripped == "" or stripped.lower() in {"null", "none", "n/a"}:
                return None
            return stripped
        return value

    @field_validator("explanation")
    @classmethod
    def _max_two_sentences(cls, value: str) -> str:
        """SPEC 7.4: explanation is at most 2 sentences. Enforced, not trusted."""
        text = value.strip()
        if not text:
            raise ValueError("explanation must not be empty")
        sentences = [s for s in _SENTENCE_END.split(text) if s and s.strip()]
        if len(sentences) > MAX_EXPLANATION_SENTENCES:
            raise ValueError(
                f"explanation must be at most {MAX_EXPLANATION_SENTENCES} sentences, "
                f"got {len(sentences)}"
            )
        return text
