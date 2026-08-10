"""AI analysis pipeline: OCR, cheap/smart model calls, output validation (SPEC 7.4)."""

from __future__ import annotations

from .client import AIClient, AIClientError, FakeAIClient, get_ai_client
from .ocr import NullOCR, OCREngine, OCRUnavailable, TesseractOCR, VisionOCR, get_ocr_engine
from .schemas import CheapResult, SmartResult

__all__ = [
    "AIClient",
    "AIClientError",
    "CheapResult",
    "FakeAIClient",
    "NullOCR",
    "OCREngine",
    "OCRUnavailable",
    "SmartResult",
    "TesseractOCR",
    "VisionOCR",
    "get_ai_client",
    "get_ocr_engine",
]
