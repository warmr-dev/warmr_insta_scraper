"""The VendorRepository interface (SPEC 7.5).

# TODO: real schema pending
#
# SPEC open question #2: the Recommend.us database schema and access method are not
# yet known. Nothing in this codebase may talk to that database directly - every
# query goes through `VendorRepository` so that swapping the stub for a real
# implementation touches this package only. Do not leak vendor-table column names
# into the bizcheck worker.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True, frozen=True)
class Vendor:
    """A candidate vendor from the Recommend.us side.

    Field names here are ours, not theirs - the real schema will be mapped onto
    this shape by the live repository implementation.
    """

    vendor_id: str
    name: str
    service_categories: tuple[str, ...] = ()
    # Free-form community label (e.g. a neighbourhood or affiliation group).
    # A lead from the *same* community is a conflict (SPEC 7.5 check 3).
    community: str | None = None
    # Geographies this vendor will service. Empty tuple means "nationwide".
    geographies: tuple[str, ...] = ()
    # Baseline Service Fit, 0-100. `service_fit()` may refine it per story.
    base_service_fit: float = 0.0
    # Set False to model an internal forwarding block (SPEC 7.5 check 5).
    accepts_forwarded_leads: bool = True
    active: bool = True
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True, frozen=True)
class StoryContext:
    """What the analyzer learned about a story, as the vendor side needs to see it.

    Assembled by the bizcheck worker from `story_analysis`; the repository never
    reads our tables itself.
    """

    story_id: str
    service_category: str | None = None
    intent_type: str | None = None
    final_score: int | None = None
    geography: str | None = None
    # Community the *lead* belongs to, compared against the vendor's (check 3).
    community: str | None = None
    ocr_text: str | None = None
    raw_analysis: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True, frozen=True)
class LeadContext:
    """Inputs to the internal forwarding rules (SPEC 7.5 check 5)."""

    story_id: str
    target_user_id: int | None = None
    username: str | None = None
    service_category: str | None = None
    final_score: int | None = None
    geography: str | None = None
    community: str | None = None


class VendorRepository(ABC):
    """Every Recommend.us query lives behind this interface.

    Implementations must be side-effect free: bizcheck calls these repeatedly and
    treats them as pure lookups.
    """

    @abstractmethod
    def find_vendor_for_category(self, service_category: str | None) -> Vendor | None:
        """Best matching active vendor for a category, or None if there is none.

        None short-circuits the whole check chain (SPEC 7.5 check 1).
        """

    @abstractmethod
    def service_fit(self, vendor_id: str, story_context: StoryContext) -> float:
        """Service Fit score, 0-100, for this vendor against this story.

        Compared against `settings.service_fit_threshold` (SPEC open question #4:
        the spec says "roughly 70-75", so the threshold is config, not a constant).
        """

    @abstractmethod
    def vendor_community(self, vendor_id: str) -> str | None:
        """The vendor's community label, or None if unknown/unaffiliated."""

    @abstractmethod
    def serves_geography(self, vendor_id: str, geography: str | None) -> bool:
        """Whether the vendor will service this geography (SPEC 7.5 check 4)."""

    @abstractmethod
    def forwarding_allowed(self, vendor_id: str, lead_context: LeadContext) -> bool:
        """Whether internal forwarding rules permit routing this lead to the vendor."""

    def close(self) -> None:
        """Release connections. Optional override."""
        return None
