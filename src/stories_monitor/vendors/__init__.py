"""Vendor lookups against the Recommend.us database (SPEC 7.5).

The real schema is unknown (SPEC open question #2), so everything goes behind
`VendorRepository`. `StubVendorRepository` backs the fixture-mode pipeline.
"""

from __future__ import annotations

from .repository import LeadContext, StoryContext, Vendor, VendorRepository
from .stub import StubVendorRepository, get_vendor_repository

__all__ = [
    "LeadContext",
    "StoryContext",
    "StubVendorRepository",
    "Vendor",
    "VendorRepository",
    "get_vendor_repository",
]
