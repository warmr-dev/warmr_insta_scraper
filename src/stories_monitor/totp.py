"""TOTP codes for 2FA-enabled worker accounts.

Instagram accepts the same 6-digit code an authenticator app shows. Holding the
underlying secret lets the poller, follower, and warden re-login unattended -
without it, a human has to type a fresh code every time a session expires,
which does not survive a multi-week follow bootstrap.

The secret is stored encrypted at rest alongside the password, and is redacted
from logs like any other credential.
"""

from __future__ import annotations

import re

from .logging_setup import get_logger

log = get_logger(__name__)

# Authenticator "setup keys" are base32; apps usually display them in groups of
# four with spaces, which we strip before use.
_B32_RE = re.compile(r"^[A-Z2-7]+=*$")


class TotpError(ValueError):
    """The stored TOTP secret is not usable."""


def normalise_secret(secret: str) -> str:
    """Strip formatting from a pasted setup key and validate it as base32."""
    cleaned = re.sub(r"[\s-]", "", secret or "").upper()
    if not cleaned:
        raise TotpError("empty TOTP secret")
    if not _B32_RE.match(cleaned):
        raise TotpError(
            "TOTP secret must be a base32 setup key (letters A-Z and digits 2-7). "
            "A 6-digit code is not a secret - pass it as verification_code instead."
        )
    return cleaned


def looks_like_code(value: str) -> bool:
    """True for a 6-digit one-time code rather than a setup key."""
    return bool(re.fullmatch(r"\d{6}", (value or "").strip()))


def current_code(secret: str) -> str:
    """The 6-digit code for `secret` right now."""
    try:
        import pyotp
    except ImportError as exc:  # pragma: no cover - dependency is pinned
        raise TotpError("pyotp is not installed") from exc

    try:
        return pyotp.TOTP(normalise_secret(secret)).now()
    except TotpError:
        raise
    except Exception as exc:  # noqa: BLE001 - pyotp raises assorted types
        raise TotpError(f"could not generate a TOTP code: {exc}") from exc


def seconds_remaining() -> int:
    """Seconds until the current 30s TOTP window rolls over.

    Used to avoid sending a code that expires mid-request.
    """
    import time

    return 30 - int(time.time()) % 30
