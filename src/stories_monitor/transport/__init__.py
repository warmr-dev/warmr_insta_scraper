"""Transport layer: the only place in the codebase that talks to Instagram.

Callers import the interface, the data carriers, the exceptions and `get_transport`
from here. No module outside this package may import instagrapi (SPEC section 11).
"""

from __future__ import annotations

from typing import Any

from ..config import get_settings
from ..logging_setup import get_logger
from .base import (
    ChallengeRequiredError,
    FeedbackRequiredError,
    InstagramTransport,
    LoginRequiredError,
    PleaseWaitError,
    PrivateAccountError,
    ProxyBlockedError,
    RateLimitedError,
    StoryItem,
    TransportError,
    TrayEntry,
    TrayResponse,
    TwoFactorRequiredError,
    UserNotFoundError,
)
from .fixture import FixtureTransport

log = get_logger(__name__)

__all__ = [
    "ChallengeRequiredError",
    "FeedbackRequiredError",
    "FixtureTransport",
    "InstagramTransport",
    "LiveTransport",
    "LoginRequiredError",
    "PleaseWaitError",
    "PrivateAccountError",
    "TwoFactorRequiredError",
    "ProxyBlockedError",
    "RateLimitedError",
    "StoryItem",
    "TransportError",
    "TrayEntry",
    "TrayResponse",
    "UserNotFoundError",
    "get_transport",
]


def __getattr__(name: str) -> Any:
    """Lazily expose `LiveTransport`.

    `live` imports instagrapi at module scope; fixture mode must not require the
    dependency to be installed (SPEC section 4).
    """
    if name == "LiveTransport":
        from .live import LiveTransport

        return LiveTransport
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def get_transport(
    username: str | None = None,
    device_settings: dict[str, Any] | None = None,
    proxy_url: str | None = None,
    **kwargs: Any,
) -> InstagramTransport:
    """Build the transport selected by `IG_TRANSPORT` (SPEC section 4).

    The poller and the fetcher call this with keyword args only:
    `username=`, `device_settings=`, `proxy_url=`.
    """
    settings = get_settings()
    if settings.is_fixture_mode:
        return FixtureTransport(
            username=username,
            device_settings=device_settings,
            proxy_url=proxy_url,
            **kwargs,
        )

    from .live import LiveTransport

    log.info("transport_live", username=username, has_proxy=bool(proxy_url))
    return LiveTransport(
        username=username,
        device_settings=device_settings,
        proxy_url=proxy_url,
        **kwargs,
    )
