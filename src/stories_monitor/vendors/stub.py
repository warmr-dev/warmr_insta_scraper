"""Fixture-backed VendorRepository (SPEC 7.5).

# TODO: real schema pending
#
# This exists so the full pipeline runs end to end without the Recommend.us
# database (SPEC open question #2). It reads `fixtures/vendors.json`. When the
# real schema lands, add a `LiveVendorRepository` alongside this and switch on
# it in `get_vendor_repository()` - nothing outside this package should change.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from ..config import get_settings
from ..logging_setup import get_logger
from .repository import LeadContext, StoryContext, Vendor, VendorRepository

log = get_logger(__name__)

_FIXTURE_FILENAME = "vendors.json"


def _normalise(value: str | None) -> str:
    """Categories, communities and geographies are matched case/space-insensitively."""
    return (value or "").strip().casefold()


class StubVendorRepository(VendorRepository):
    """Serves vendors, fit scores and forwarding rules out of a JSON fixture."""

    def __init__(self, fixture_path: str | Path | None = None) -> None:
        self._path = Path(fixture_path) if fixture_path else self._default_path()
        raw = self._load(self._path)

        self._vendors: dict[str, Vendor] = {}
        for entry in raw.get("vendors", []):
            vendor = Vendor(
                vendor_id=entry["vendor_id"],
                name=entry.get("name", entry["vendor_id"]),
                service_categories=tuple(entry.get("service_categories", [])),
                community=entry.get("community"),
                geographies=tuple(entry.get("geographies", [])),
                base_service_fit=float(entry.get("base_service_fit", 0.0)),
                accepts_forwarded_leads=bool(entry.get("accepts_forwarded_leads", True)),
                active=bool(entry.get("active", True)),
                raw=entry,
            )
            self._vendors[vendor.vendor_id] = vendor

        self._fit_adjustments: dict[str, float] = {
            key: float(val)
            for key, val in raw.get("category_fit_adjustments", {}).items()
            if not key.startswith("_") and isinstance(val, (int, float))
        }

        rules: dict[str, Any] = raw.get("forwarding_rules", {})
        self._blocked_categories = {
            _normalise(c) for c in rules.get("blocked_categories", [])
        }
        self._blocked_vendor_geographies = {
            vendor_id: {_normalise(g) for g in geos}
            for vendor_id, geos in rules.get("blocked_vendor_geographies", {}).items()
        }
        self._min_score_for_forwarding = int(rules.get("min_score_for_forwarding", 0))

        log.info(
            "vendor_stub_loaded",
            fixture=str(self._path),
            vendor_count=len(self._vendors),
            active_count=sum(1 for v in self._vendors.values() if v.active),
        )

    # --- fixture loading ---

    @staticmethod
    def _default_path() -> Path:
        return Path(get_settings().fixtures_dir) / _FIXTURE_FILENAME

    @staticmethod
    def _load(path: Path) -> dict[str, Any]:
        if not path.exists():
            raise FileNotFoundError(
                f"Vendor fixture not found at {path}. "
                "The stub repository requires fixtures/vendors.json (SPEC 7.5)."
            )
        with path.open(encoding="utf-8") as fh:
            return json.load(fh)

    # --- VendorRepository ---

    def find_vendor_for_category(self, service_category: str | None) -> Vendor | None:
        """Highest base fit among active vendors listing this category.

        An unknown or unlisted category yields None, which short-circuits the
        chain at check 1.
        """
        wanted = _normalise(service_category)
        if not wanted:
            return None

        candidates = [
            v
            for v in self._vendors.values()
            if v.active and any(_normalise(c) == wanted for c in v.service_categories)
        ]
        if not candidates:
            return None
        # Deterministic: best fit first, vendor_id breaks ties so tests are stable.
        return max(candidates, key=lambda v: (v.base_service_fit, v.vendor_id))

    def service_fit(self, vendor_id: str, story_context: StoryContext) -> float:
        """Base fit nudged by intent type, clamped to 0-100."""
        vendor = self._vendors.get(vendor_id)
        if vendor is None:
            return 0.0
        adjustment = self._fit_adjustments.get(story_context.intent_type or "", 0.0)
        return max(0.0, min(100.0, vendor.base_service_fit + adjustment))

    def vendor_community(self, vendor_id: str) -> str | None:
        vendor = self._vendors.get(vendor_id)
        return vendor.community if vendor else None

    def serves_geography(self, vendor_id: str, geography: str | None) -> bool:
        """An empty geography list means nationwide.

        An unknown lead geography is treated as serviceable - we do not reject a
        lead for a field the AI simply failed to extract.
        """
        vendor = self._vendors.get(vendor_id)
        if vendor is None:
            return False
        if not vendor.geographies:
            return True
        wanted = _normalise(geography)
        if not wanted:
            return True
        return any(wanted == _normalise(g) for g in vendor.geographies)

    def forwarding_allowed(self, vendor_id: str, lead_context: LeadContext) -> bool:
        vendor = self._vendors.get(vendor_id)
        if vendor is None or not vendor.accepts_forwarded_leads:
            return False
        if _normalise(lead_context.service_category) in self._blocked_categories:
            return False
        # An unknown lead geography can never match a blocked one.
        lead_geo = _normalise(lead_context.geography)
        if lead_geo and lead_geo in self._blocked_vendor_geographies.get(vendor_id, set()):
            return False
        if (
            lead_context.final_score is not None
            and lead_context.final_score < self._min_score_for_forwarding
        ):
            return False
        return True


_repo: VendorRepository | None = None
_repo_lock = threading.Lock()


def get_vendor_repository() -> VendorRepository:
    """Process-wide repository singleton.

    # TODO: real schema pending - when the Recommend.us access method is known,
    # branch here on config to return a LiveVendorRepository outside fixture mode.
    """
    global _repo
    if _repo is None:
        with _repo_lock:
            if _repo is None:
                _repo = StubVendorRepository()
    return _repo


def reset_vendor_repository() -> None:
    """Test hook - drops the cached repository so a new fixture path takes effect."""
    global _repo
    _repo = None
