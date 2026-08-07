"""The InstagramTransport interface.

Every Instagram call in this system goes through this interface. No module outside
`stories_monitor.transport` may import instagrapi (SPEC section 11).

Errors are re-raised as transport-level exceptions so that callers never need to
catch instagrapi types.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

# --- Transport-level exceptions (instagrapi types never escape the transport) ---


class TransportError(Exception):
    """Base for all transport errors."""


class ChallengeRequiredError(TransportError):
    """Account hit a challenge. NEVER auto-solve - alert a human (SPEC 7.8)."""


class LoginRequiredError(TransportError):
    """Session rejected. Warden may attempt exactly one re-login."""


class FeedbackRequiredError(TransportError):
    """Action blocked. Back off; stop writes for this account."""


class PleaseWaitError(TransportError):
    """Soft rate limit. Back off 5-30 min with jitter."""


class RateLimitedError(TransportError):
    """HTTP 429."""


class PrivateAccountError(TransportError):
    """Target is private and we do not follow them."""


class UserNotFoundError(TransportError):
    """Target no longer exists."""


class ProxyBlockedError(TransportError):
    """Proxy address rejected by Instagram."""


# --- Data carried across the boundary ---


@dataclass(slots=True)
class TrayEntry:
    """One entry in the reels_tray response.

    `id` may be a numeric user pk or a `highlight:...` string - the poller filters
    non-numeric ids out (SPEC 7.1).
    """

    id: str
    latest_reel_media: int | None = None
    seen: int | None = None
    user: dict[str, Any] = field(default_factory=dict)
    # Some trays prefetch the story items; when present the fetcher can skip reels_media.
    items: list[dict[str, Any]] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def is_user_entry(self) -> bool:
        """True only for real user entries; filters out `highlight:1234...`."""
        return str(self.id).isdigit()

    @property
    def user_id(self) -> int | None:
        return int(self.id) if self.is_user_entry else None

    @property
    def has_prefetched_items(self) -> bool:
        return bool(self.items)


@dataclass(slots=True)
class TrayResponse:
    entries: list[TrayEntry]
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def next_max_id(self) -> str | None:
        """Cursor for pagination, if the tray is truncated (SPEC 7.2)."""
        return self.raw.get("next_max_id") or None

    @property
    def entry_count(self) -> int:
        """Truncation canary - a sudden drop means trouble (SPEC section 10)."""
        return len(self.entries)


@dataclass(slots=True)
class StoryItem:
    """One story media item from reels_media."""

    story_id: str
    user_id: int
    taken_at: int
    media_type: int  # 1 = photo, 2 = video
    expiring_at: int | None = None
    image_versions: list[dict[str, Any]] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def is_photo(self) -> bool:
        return self.media_type == 1

    def best_image_url(self) -> str | None:
        """Largest image_versions2 candidate by area."""
        if not self.image_versions:
            return None
        best = max(
            self.image_versions,
            key=lambda c: (c.get("width") or 0) * (c.get("height") or 0),
        )
        return best.get("url")


# --- The interface ---


class InstagramTransport(ABC):
    """One interface, two implementations: LiveTransport and FixtureTransport."""

    @abstractmethod
    def login(self, username: str, password: str, **kwargs: Any) -> dict[str, Any]:
        """Log in and return session settings suitable for persisting."""

    @abstractmethod
    def load_session(self, session_json: dict[str, Any]) -> None:
        """Restore a persisted session. Avoids a fresh login (SPEC section 8)."""

    @abstractmethod
    def dump_session(self) -> dict[str, Any]:
        """Export the current session for persistence."""

    @abstractmethod
    def reels_tray(
        self, *, cold_start: bool = False, max_id: str | None = None
    ) -> TrayResponse:
        """Fetch the story tray for all of this account's followings.

        This is the core architectural call: one request covers thousands of
        monitored targets (SPEC section 1).
        """

    @abstractmethod
    def reels_media(self, user_ids: list[int]) -> dict[int, list[StoryItem]]:
        """Batch-fetch story items for up to ~50 user ids."""

    @abstractmethod
    def user_follow(self, user_id: int) -> bool:
        """True on a new follow or new outgoing request; False if already following/pending."""

    @abstractmethod
    def user_friendship(self, user_id: int) -> dict[str, Any]:
        """Friendship status - used to promote `requested` -> `following`."""

    @abstractmethod
    def download_media(self, url: str, dest_path: str) -> str:
        """Download media to a temp path. Caller MUST delete it in a finally block."""

    def close(self) -> None:
        """Release resources. Optional override."""
        return None
