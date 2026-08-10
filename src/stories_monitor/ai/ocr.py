"""OCR abstraction with two real backends (SPEC 7.4 step 1).

The spec says to measure Tesseract and the cheap model's vision during the pilot
and keep the cheaper one, so both are real implementations behind one protocol.
`NullOCR` exists for fixture-mode tests where neither backend is available.

Never log image bytes or transcribed contents beyond a length counter.
"""

from __future__ import annotations

import base64
import json
import mimetypes
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from ..config import get_settings
from ..logging_setup import get_logger

if TYPE_CHECKING:  # pragma: no cover - typing only, avoids an import cycle
    from .client import AIClient

logger = get_logger(__name__)

# Anthropic vision accepts these media types.
_SUPPORTED_MEDIA_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}
_DEFAULT_MEDIA_TYPE = "image/jpeg"


class OCRUnavailable(RuntimeError):
    """The selected OCR backend cannot run (missing package or binary)."""


@runtime_checkable
class OCREngine(Protocol):
    """One interface, several backends. Selected by settings.ocr_engine."""

    name: str

    def extract_text(self, image_path: str) -> str:
        """Return text visible in the image. Empty string when there is none."""
        ...


def encode_image(image_path: str) -> tuple[str, str]:
    """Return (media_type, base64_data) for an image on disk.

    Shared by VisionOCR and the AI client so both encode identically.
    """
    path = Path(image_path)
    if not path.is_file():
        raise OCRUnavailable(f"image not found: {image_path}")

    guessed, _ = mimetypes.guess_type(path.name)
    media_type = guessed if guessed in _SUPPORTED_MEDIA_TYPES else _DEFAULT_MEDIA_TYPE
    data = base64.standard_b64encode(path.read_bytes()).decode("ascii")
    return media_type, data


class TesseractOCR:
    """Local Tesseract via pytesseract.

    pytesseract needs both the Python package and the `tesseract` binary. Either
    may be missing on a dev box, so both failure modes raise OCRUnavailable with a
    clear message rather than an opaque ImportError deep in the analyzer.
    """

    name = "tesseract"

    def __init__(self, lang: str = "eng") -> None:
        self.lang = lang

    def extract_text(self, image_path: str) -> str:
        try:
            import pytesseract
            from PIL import Image
        except ImportError as exc:  # pragma: no cover - environment dependent
            raise OCRUnavailable(
                "pytesseract/Pillow not installed; install them or set OCR_ENGINE=vision"
            ) from exc

        try:
            with Image.open(image_path) as img:
                text = pytesseract.image_to_string(img, lang=self.lang)
        except pytesseract.TesseractNotFoundError as exc:
            raise OCRUnavailable(
                "tesseract binary not found on PATH; install tesseract-ocr or set "
                "OCR_ENGINE=vision"
            ) from exc
        except FileNotFoundError as exc:
            raise OCRUnavailable(f"image not found: {image_path}") from exc
        except Exception as exc:  # noqa: BLE001 - backend failures must be typed
            raise OCRUnavailable(f"tesseract failed: {exc}") from exc

        cleaned = text.strip()
        logger.debug("ocr_done", engine=self.name, chars=len(cleaned))
        return cleaned


class VisionOCR:
    """Reads text using the cheap model's vision.

    Takes the AIClient lazily so the analyzer can share one client (and one set of
    spend metrics) between OCR and classification.
    """

    name = "vision"

    def __init__(self, client: AIClient | None = None) -> None:
        self._client = client

    def _get_client(self) -> AIClient:
        if self._client is None:
            from .client import get_ai_client

            self._client = get_ai_client()
        return self._client

    def extract_text(self, image_path: str) -> str:
        text = self._get_client().read_text(image_path)
        cleaned = text.strip()
        logger.debug("ocr_done", engine=self.name, chars=len(cleaned))
        return cleaned


class NullOCR:
    """No-op backend for fixture-mode tests.

    Returns a sidecar `<image>.ocr.txt` when one exists so fixtures can pin OCR
    text deterministically; otherwise the empty string.
    """

    name = "null"

    def extract_text(self, image_path: str) -> str:
        sidecar = Path(f"{image_path}.ocr.txt")
        if sidecar.is_file():
            return sidecar.read_text(encoding="utf-8").strip()
        # Fixture images may carry OCR text in a JSON sidecar too.
        json_sidecar = Path(image_path).with_suffix(".json")
        if json_sidecar.is_file():
            try:
                payload = json.loads(json_sidecar.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return ""
            value = payload.get("ocr_text") if isinstance(payload, dict) else None
            return str(value).strip() if value else ""
        return ""


def get_ocr_engine(client: AIClient | None = None) -> OCREngine:
    """Build the engine named by settings.ocr_engine.

    In fixture mode without an API key the vision backend cannot reach Anthropic,
    so fall back to NullOCR rather than failing the whole pipeline.
    """
    settings = get_settings()

    if settings.ocr_engine == "vision":
        if not settings.anthropic_api_key:
            logger.info("ocr_engine_fallback", requested="vision", using="null")
            return NullOCR()
        return VisionOCR(client=client)

    return TesseractOCR()
