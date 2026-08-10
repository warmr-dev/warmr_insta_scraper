"""One-shot login smoke test.

Proves the auth path works end to end - credentials, device settings, and the
bound proxy - and nothing more. It makes exactly one login call and saves the
resulting session, because repeated logins are the single strongest ban signal
(SPEC section 8).

If a saved session already exists it is reused and NO login happens at all.

This module never calls media/seen/ or any write endpoint against a target
(SPEC section 11).
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from typing import Any

from sqlalchemy import select, update

from .crypto import SecretBox
from .db.models import AccountEvent, WorkerAccount
from .db.session import session_scope
from .logging_setup import get_logger
from .transport import (
    ChallengeRequiredError,
    FeedbackRequiredError,
    LoginRequiredError,
    PleaseWaitError,
    ProxyBlockedError,
    TransportError,
    TwoFactorRequiredError,
    get_transport,
)

log = get_logger(__name__)


def run_login_test(
    username: str | None = None,
    verification_code: str | None = None,
    code_prompt: Callable[[], str] | None = None,
    allow_no_proxy: bool = False,
) -> dict[str, Any]:
    """Log the account in once through its bound proxy and persist the session.

    `verification_code` is the 6-digit TOTP from the account's authenticator app
    when 2FA is enabled. Codes expire in ~30s, so read it immediately before
    running this.
    """
    with session_scope() as session:
        stmt = select(WorkerAccount)
        if username:
            stmt = stmt.where(WorkerAccount.username == username)
        account = session.scalars(stmt.order_by(WorkerAccount.id).limit(1)).first()
        if account is None:
            raise RuntimeError(
                "no worker account found - run `stories seed-worker` first"
            )
        account_id = account.id
        account_username = account.username
        device_settings = dict(account.device_settings or {})
        proxy_url = account.proxy_url
        session_json = dict(account.session_json) if account.session_json else None
        password_enc = account.password_enc

    if not proxy_url and not allow_no_proxy:
        raise RuntimeError(
            "account has no bound proxy; refusing to log in from the local IP "
            "(SPEC section 8 binds a proxy for the account's whole life). "
            "Pass allow_no_proxy=True to override for local testing."
        )
    if not proxy_url:
        # Deliberate: the local IP becomes this account's bound identity for as
        # long as it is used. Switching to a proxy later is itself a ban signal,
        # so this is a testing mode, not a path to production (SPEC section 8).
        log.warning(
            "no_proxy_bound",
            username=account_username,
            detail="logging in from the local IP; this IP is now this account's identity",
        )

    transport = get_transport(
        username=account_username,
        device_settings=device_settings,
        proxy_url=proxy_url,
    )

    # A saved session means we have already paid the login cost - do not pay it
    # again just to run a test.
    if session_json:
        transport.load_session(session_json)
        log.info("session_reused", username=account_username)
        return {
            "username": account_username,
            "result": "session_reused",
            "logged_in": False,
            "detail": "an existing session was restored; no login was performed",
        }

    password = SecretBox().decrypt(password_enc)

    # Everything slow - the DB read, transport construction, proxy binding - is
    # already done. Only now do we ask for a code, so a 30s TOTP window is spent
    # on the request itself rather than on setup.
    code = _resolve_code(verification_code, code_prompt)
    login_kwargs: dict[str, Any] = {}
    if code:
        login_kwargs["verification_code"] = code

    try:
        dumped = transport.login(account_username, password, **login_kwargs)
    except TwoFactorRequiredError as exc:
        # Not a credential failure - the account simply needs a TOTP code.
        _record(account_id, status=None, event="login_required", detail=str(exc))
        return {
            "username": account_username,
            "result": "two_factor_required",
            "logged_in": False,
            "detail": str(exc),
            "next_step": (
                "Re-run with --verification-code <6 digits> from the authenticator "
                "app. Read the code immediately before running; it expires in ~30s."
            ),
        }
    except ChallengeRequiredError as exc:
        _record(account_id, status="challenged", event="challenge", detail=str(exc))
        return {
            "username": account_username,
            "result": "challenge_required",
            "logged_in": False,
            "detail": str(exc),
            "next_step": (
                "A human must clear the challenge in the Instagram app on the same "
                "proxy. NEVER auto-solve it (SPEC 7.8)."
            ),
        }
    except (FeedbackRequiredError, PleaseWaitError) as exc:
        _record(account_id, status=None, event="feedback_required", detail=str(exc))
        return {
            "username": account_username,
            "result": "rate_limited",
            "logged_in": False,
            "detail": str(exc),
            "next_step": "Back off and retry later; the account is still usable.",
        }
    except ProxyBlockedError as exc:
        _record(account_id, status=None, event="login_required", detail=str(exc))
        return {
            "username": account_username,
            "result": "proxy_blocked",
            "logged_in": False,
            "detail": str(exc),
            "next_step": "Instagram rejected the proxy IP. A different proxy is needed.",
        }
    except (LoginRequiredError, TransportError) as exc:
        _record(account_id, status=None, event="login_required", detail=str(exc))
        return {
            "username": account_username,
            "result": "login_failed",
            "logged_in": False,
            "detail": str(exc),
            "next_step": (
                "If this account has 2FA enabled, the code may have expired - "
                "re-run with a freshly read --verification-code. Otherwise check "
                "the password, or that the account is not pending verification."
            ),
        }

    _persist_session(account_id, dumped)
    log.info("login_ok", username=account_username)
    return {
        "username": account_username,
        "result": "ok",
        "logged_in": True,
        "detail": "session saved; future runs will reuse it instead of logging in",
    }


def _resolve_code(
    verification_code: str | None, code_prompt: Callable[[], str] | None = None
) -> str:
    """Resolve a 2FA code: explicit value, stored secret, or an interactive ask.

    Order matters. A stored secret is strictly better than a typed code - it
    lets the follower and warden re-login unattended over the weeks the follow
    bootstrap takes - so it wins over prompting a human.
    """
    from .config import get_settings
    from .totp import TotpError, current_code, looks_like_code, seconds_remaining

    supplied = (verification_code or "").strip()
    if supplied:
        # Tolerate someone pasting the setup key into the code flag.
        if not looks_like_code(supplied):
            try:
                return current_code(supplied)
            except TotpError:
                return supplied
        return supplied

    secret = get_settings().ig_worker_totp_secret
    if secret:
        code = current_code(secret)
        # Do not send a code that expires mid-flight; wait for the next window.
        if seconds_remaining() < 5:
            import time

            time.sleep(seconds_remaining() + 1)
            code = current_code(secret)
        log.info("totp_generated", seconds_remaining=seconds_remaining())
        return code

    if code_prompt is not None:
        return (code_prompt() or "").strip()
    return ""


def _persist_session(account_id: int, session_json: dict[str, Any]) -> None:
    with session_scope() as session:
        session.execute(
            update(WorkerAccount)
            .where(WorkerAccount.id == account_id)
            .values(
                session_json=session_json,
                last_login_at=dt.datetime.now(dt.UTC),
                last_error=None,
                status="active",
            )
        )


def _record(account_id: int, *, status: str | None, event: str, detail: str) -> None:
    with session_scope() as session:
        values: dict[str, Any] = {"last_error": detail[:1000]}
        if status:
            values["status"] = status
        session.execute(
            update(WorkerAccount).where(WorkerAccount.id == account_id).values(**values)
        )
        session.add(
            AccountEvent(
                worker_account_id=account_id, event_type=event, detail=detail[:2000]
            )
        )
