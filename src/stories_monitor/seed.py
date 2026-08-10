"""Seeding a worker account into the database (offline).

Generates the account's device settings ONCE and stores them, encrypts the
password at rest, and binds the proxy permanently. Nothing here talks to
Instagram - the account only makes a network call when the poller or a smoke
test logs it in.

SPEC section 8: device_settings are generated once and never regenerated; the
proxy is bound permanently and never rotated. Re-seeding an existing account
therefore refuses to touch either unless explicitly forced.
"""

from __future__ import annotations

import hashlib
import random
from typing import Any

from sqlalchemy import select

from .config import get_settings
from .crypto import SecretBox
from .db.models import WorkerAccount
from .db.session import session_scope
from .logging_setup import get_logger

log = get_logger(__name__)

# A small pool of plausible mid-range Android devices. The point is a coherent,
# stable fingerprint per account - not variety for its own sake.
_DEVICE_POOL: list[dict[str, Any]] = [
    {
        "app_version": "269.0.0.18.75",
        "android_version": 31,
        "android_release": "12.0",
        "dpi": "420dpi",
        "resolution": "1080x2340",
        "manufacturer": "samsung",
        "device": "a52sxq",
        "model": "SM-A528B",
        "cpu": "qcom",
        "version_code": "436385779",
    },
    {
        "app_version": "269.0.0.18.75",
        "android_version": 33,
        "android_release": "13.0",
        "dpi": "440dpi",
        "resolution": "1080x2400",
        "manufacturer": "Xiaomi",
        "device": "sweet",
        "model": "M2101K6G",
        "cpu": "qcom",
        "version_code": "436385779",
    },
    {
        "app_version": "269.0.0.18.75",
        "android_version": 32,
        "android_release": "12.1",
        "dpi": "420dpi",
        "resolution": "1080x2400",
        "manufacturer": "Google",
        "device": "oriole",
        "model": "Pixel 6",
        "cpu": "exynos",
        "version_code": "436385779",
    },
]


def build_device_settings(username: str, timezone: str = "America/Los_Angeles") -> dict[str, Any]:
    """Deterministically derive a device fingerprint from the username.

    Deterministic on purpose: if the row is ever lost before first login, the
    same username regenerates the same device rather than a new identity.
    Once the row exists, this is never called again for that account.
    """
    seed = int(hashlib.sha256(username.encode()).hexdigest()[:16], 16)
    rng = random.Random(seed)

    device = dict(rng.choice(_DEVICE_POOL))
    device["locale"] = "en_US"
    device["timezone"] = timezone
    # Instagram sends the offset in seconds; keep it consistent with `timezone`.
    device["timezone_offset"] = -28800  # America/Los_Angeles standard time
    device["user_agent"] = (
        f"Instagram {device['app_version']} Android "
        f"({device['android_version']}/{device['android_release']}; "
        f"{device['dpi']}; {device['resolution']}; {device['manufacturer']}; "
        f"{device['model']}; {device['device']}; {device['cpu']}; en_US; "
        f"{device['version_code']})"
    )
    return device


def seed_worker(
    *,
    username: str,
    password: str,
    proxy_url: str,
    shard_id: int = 0,
    status: str = "warming",
    timezone: str = "America/Los_Angeles",
    force_device: bool = False,
    allow_no_proxy: bool = False,
) -> dict[str, Any]:
    """Insert or update a worker account. Returns a small report.

    An existing account keeps its device_settings and proxy_url: regenerating
    either is the fastest way to burn it (SPEC section 8).
    """
    if not username or not password:
        raise ValueError("username and password are required")
    if not proxy_url and not allow_no_proxy:
        raise ValueError(
            "proxy_url is required - an account must be bound to a proxy before "
            "its first login (SPEC section 8). Pass allow_no_proxy=True to bind "
            "the local IP instead (testing only)."
        )
    if not proxy_url:
        # The local IP becomes this account's identity for as long as it is used;
        # switching to a proxy later is itself a ban signal (SPEC section 8).
        log.warning(
            "seeding_without_proxy",
            username=username,
            detail="account will be bound to the local IP",
        )

    box = SecretBox()
    password_enc = box.encrypt(password)

    with session_scope() as session:
        existing = session.scalars(
            select(WorkerAccount).where(WorkerAccount.username == username)
        ).first()

        if existing is None:
            device = build_device_settings(username, timezone=timezone)
            account = WorkerAccount(
                username=username,
                password_enc=password_enc,
                shard_id=shard_id,
                proxy_url=proxy_url,
                device_settings=device,
                status=status,
            )
            session.add(account)
            session.flush()
            report = {
                "action": "created",
                "worker_account_id": account.id,
                "username": username,
                "shard_id": shard_id,
                "status": status,
                "device_model": device["model"],
                "proxy_host": _proxy_host(proxy_url),
            }
            log.info("worker_seeded", **report)
            return report

        # Update the password only; identity fields are immutable.
        existing.password_enc = password_enc
        changed = ["password_enc"]

        if existing.proxy_url != proxy_url:
            if force_device:
                existing.proxy_url = proxy_url
                changed.append("proxy_url")
                log.warning(
                    "proxy_rotated",
                    username=username,
                    detail="SPEC section 8 forbids this outside recovery",
                )
            else:
                log.warning(
                    "proxy_change_refused",
                    username=username,
                    detail="existing proxy kept; pass force_device to override",
                )

        if force_device:
            existing.device_settings = build_device_settings(username, timezone=timezone)
            changed.append("device_settings")
            log.warning("device_settings_regenerated", username=username)

        report = {
            "action": "updated",
            "worker_account_id": existing.id,
            "username": username,
            "shard_id": existing.shard_id,
            "status": existing.status,
            "changed": changed,
            "device_model": (existing.device_settings or {}).get("model"),
            "proxy_host": _proxy_host(existing.proxy_url),
        }
        log.info("worker_seeded", **report)
        return report


def seed_from_env() -> dict[str, Any]:
    """Seed the worker described by IG_WORKER_* environment variables."""
    settings = get_settings()
    return seed_worker(
        username=settings.ig_worker_username,
        password=settings.ig_worker_password,
        proxy_url=settings.ig_worker_proxy_url,
    )


def _proxy_host(proxy_url: str | None) -> str:
    """Host:port of a proxy URL, with credentials stripped - safe to log."""
    if not proxy_url:
        return ""
    tail = proxy_url.rsplit("@", 1)[-1]
    return tail
