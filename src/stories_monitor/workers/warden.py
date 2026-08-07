"""The warden (SPEC 7.8) - account health, alerts, recovery orchestration.

The warden is the only process allowed to change a worker account's status. It has
one prime directive, enforced in `handle_account_error` and asserted in
`assert_never_auto_solve`: a challenge is NEVER auto-solved. Attempting to solve a
challenge programmatically reliably converts a recoverable account into a banned
one. The correct response is to stop the account and alert a human.
"""

from __future__ import annotations

import datetime as dt
import random
import time
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from ..config import Q_ANALYZE, Q_BIZCHECK, Q_FETCH, Q_FETCH_DIRECT, Q_NOTIFY, get_settings
from ..crypto import SecretBox
from ..db.models import AccountEvent, MetricSample, Target, TargetFollow, WorkerAccount
from ..db.session import session_scope
from ..logging_setup import get_logger
from ..metrics import record_metric
from ..queue import get_queue
from ..transport import (
    ChallengeRequiredError,
    FeedbackRequiredError,
    LoginRequiredError,
    PleaseWaitError,
    ProxyBlockedError,
    RateLimitedError,
    TransportError,
    get_transport,
)
from ._common import _mark_account, log_account_event

log = get_logger(__name__)

# SPEC 7.8: exactly ONE re-login attempt. Two consecutive failures -> 'challenged'.
MAX_CONSECUTIVE_LOGIN_FAILURES = 2

# How far back a `login_required` event still counts toward the consecutive streak.
LOGIN_FAILURE_WINDOW_SEC = 6 * 3600

# SPEC 7.8: feedback_required / please_wait back off 5-30 minutes with jitter.
BACKOFF_MIN_SEC = 300
BACKOFF_MAX_SEC = 1800

# Statuses that mean the account can no longer do work for its shard.
LOST_STATUSES = ("challenged", "banned")

QUEUE_DEPTH_ALERT_THRESHOLD = 5000

MONITORED_QUEUES = (Q_FETCH, Q_FETCH_DIRECT, Q_ANALYZE, Q_BIZCHECK, Q_NOTIFY)


@dataclass(slots=True)
class Alert:
    """One structured alert condition. Returned by `check_health` so both the warden
    loop and the CLI can render the same list without duplicating the checks."""

    kind: str
    severity: str  # warning | critical
    message: str
    context: dict[str, Any] = field(default_factory=dict)

    def emit(self) -> None:
        """Log with alert=True so downstream log routing can page on it (SPEC 10)."""
        logger = log.error if self.severity == "critical" else log.warning
        logger("alert", alert=True, kind=self.kind, severity=self.severity,
               message=self.message, **self.context)
        record_metric("alerts_raised", 1, {"kind": self.kind, "severity": self.severity})


@dataclass(slots=True)
class RecoveryAction:
    """What the warden decided to do about one account error. Test-assertable."""

    worker_account_id: int
    action: str  # challenged | relogin_ok | relogin_failed | backoff | noop
    backoff_sec: float = 0.0
    detail: str | None = None


class Warden:
    """Watches account health, performs the one permitted recovery, raises alerts."""

    def __init__(self) -> None:
        self.settings = get_settings()
        self._stopped = False

    # --- the prime directive ---

    @staticmethod
    def assert_never_auto_solve() -> None:
        """There is deliberately NO challenge-solving code path in this system.

        Do not add one. `instagrapi`'s challenge_resolve, SMS/email code entry, and
        any equivalent are forbidden (SPEC 7.8 and SPEC section 11): auto-solving a
        challenge reliably turns a recoverable account into a permanently banned one.
        A challenged account stops, waits for a human, and its shard gets a reserve.
        """
        return None

    # --- error handling ---

    def handle_account_error(self, worker_account_id: int, exc: Exception) -> RecoveryAction:
        """Single entry point for every transport failure reported against an account."""
        error = str(exc)

        if isinstance(exc, ChallengeRequiredError):
            return self._handle_challenge(worker_account_id, error)

        if isinstance(exc, ProxyBlockedError):
            # A blocked proxy is unrecoverable for this identity: the proxy is bound
            # permanently and must NOT be rotated (SPEC section 8), so the account is
            # lost and its shard needs a reserve.
            self._retire_account(worker_account_id, error, event="ban")
            return RecoveryAction(worker_account_id, "challenged", detail=error[:200])

        if isinstance(exc, LoginRequiredError):
            return self._handle_login_required(worker_account_id, error)

        if isinstance(exc, (FeedbackRequiredError, PleaseWaitError, RateLimitedError)):
            backoff = random.uniform(BACKOFF_MIN_SEC, BACKOFF_MAX_SEC)
            # Keep the account ACTIVE - this is a soft limit, not a lost account.
            _mark_account(
                worker_account_id, status=None, error=error, event="feedback_required"
            )
            log.warning(
                "warden_backoff",
                worker_account_id=worker_account_id,
                backoff_sec=int(backoff),
            )
            return RecoveryAction(
                worker_account_id, "backoff", backoff_sec=backoff, detail=error[:200]
            )

        if isinstance(exc, TransportError):
            log_account_event(worker_account_id, "login_required", f"transport error: {error}")
            log.warning(
                "warden_transport_error",
                worker_account_id=worker_account_id,
                error=error[:200],
            )
            return RecoveryAction(worker_account_id, "noop", detail=error[:200])

        log.exception("warden_unexpected_error", worker_account_id=worker_account_id)
        return RecoveryAction(worker_account_id, "noop", detail=error[:200])

    def _handle_challenge(self, worker_account_id: int, error: str) -> RecoveryAction:
        # NEVER attempt to auto-solve (SPEC 7.8). Stop the account, alert a human.
        self.assert_never_auto_solve()
        self._retire_account(worker_account_id, error, event="challenge")
        Alert(
            kind="account_challenged",
            severity="critical",
            message="Worker account hit a challenge - human action required, do NOT auto-solve",
            context={"worker_account_id": worker_account_id},
        ).emit()
        return RecoveryAction(worker_account_id, "challenged", detail=error[:200])

    def _retire_account(self, worker_account_id: int, error: str, *, event: str) -> None:
        """Mark the account challenged, stopping ALL its activity, then backfill.

        Both the poller and the follower gate their work on `status == 'active'`, so
        flipping the status here is what actually stops the account everywhere.
        """
        with session_scope() as session:
            shard_id = session.scalar(
                select(WorkerAccount.shard_id).where(WorkerAccount.id == worker_account_id)
            )
        _mark_account(worker_account_id, status="challenged", error=error, event=event)
        record_metric("accounts_lost", 1, {"worker_account_id": worker_account_id})
        if shard_id is not None:
            self.promote_reserve(int(shard_id))

    # --- the one permitted recovery: exactly one re-login ---

    def _consecutive_login_failures(self, worker_account_id: int) -> int:
        """Count recent `login_required` events since the last successful recovery.

        The streak lives in `account_events` rather than in memory so a warden
        restart cannot hand an account a fresh set of login attempts.
        """
        cutoff = dt.datetime.now(dt.UTC) - dt.timedelta(seconds=LOGIN_FAILURE_WINDOW_SEC)
        with session_scope() as session:
            rows = session.execute(
                select(AccountEvent.event_type, AccountEvent.occurred_at)
                .where(
                    AccountEvent.worker_account_id == worker_account_id,
                    AccountEvent.occurred_at >= cutoff,
                    AccountEvent.event_type.in_(("login_required", "recovered")),
                )
                .order_by(AccountEvent.occurred_at.desc())
            ).all()

        failures = 0
        for event_type, _occurred_at in rows:
            if event_type == "recovered":
                break  # a success resets the streak
            failures += 1
        return failures

    def _handle_login_required(self, worker_account_id: int, error: str) -> RecoveryAction:
        log_account_event(worker_account_id, "login_required", error)

        if self._consecutive_login_failures(worker_account_id) >= MAX_CONSECUTIVE_LOGIN_FAILURES:
            # SPEC 7.8: two consecutive failures -> challenged. Do not keep retrying;
            # repeated logins are the single strongest ban signal (SPEC section 8).
            self._retire_account(worker_account_id, error, event="challenge")
            Alert(
                kind="account_login_exhausted",
                severity="critical",
                message="Two consecutive login failures - account marked challenged",
                context={"worker_account_id": worker_account_id},
            ).emit()
            return RecoveryAction(worker_account_id, "challenged", detail=error[:200])

        return self.attempt_relogin(worker_account_id)

    def attempt_relogin(self, worker_account_id: int) -> RecoveryAction:
        """Exactly ONE re-login, with the SAME device_settings and SAME proxy.

        Never regenerate device settings, never rotate the proxy (SPEC section 8) -
        both are read straight back out of the row and passed through unchanged.
        """
        with session_scope() as session:
            account = session.get(WorkerAccount, worker_account_id)
            if account is None:
                return RecoveryAction(worker_account_id, "noop", detail="account not found")
            username = account.username
            device_settings = dict(account.device_settings or {})  # immutable, reused as-is
            proxy_url = account.proxy_url  # permanently bound, reused as-is
            password_enc = account.password_enc

        try:
            transport = get_transport(
                username=username,
                device_settings=device_settings,
                proxy_url=proxy_url,
            )
            password = SecretBox().decrypt(password_enc)
            dumped = transport.login(username, password)
        except ChallengeRequiredError as exc:
            # A challenge during re-login is still a challenge. Never auto-solve.
            return self._handle_challenge(worker_account_id, str(exc))
        except Exception as exc:  # noqa: BLE001 - any failure counts as one failure
            log_account_event(worker_account_id, "login_required", f"relogin failed: {exc}")
            log.warning(
                "relogin_failed", worker_account_id=worker_account_id, error=str(exc)[:200]
            )
            if self._consecutive_login_failures(worker_account_id) >= MAX_CONSECUTIVE_LOGIN_FAILURES:
                self._retire_account(worker_account_id, str(exc), event="challenge")
                return RecoveryAction(worker_account_id, "challenged", detail=str(exc)[:200])
            return RecoveryAction(worker_account_id, "relogin_failed", detail=str(exc)[:200])

        with session_scope() as session:
            session.execute(
                update(WorkerAccount)
                .where(WorkerAccount.id == worker_account_id)
                .values(
                    session_json=dumped,
                    last_login_at=dt.datetime.now(dt.UTC),
                    last_error=None,
                )
            )
            session.add(
                AccountEvent(
                    worker_account_id=worker_account_id,
                    event_type="recovered",
                    detail="re-login succeeded with original device settings and proxy",
                )
            )
        record_metric("relogin_success", 1, {"worker_account_id": worker_account_id})
        log.info("relogin_succeeded", worker_account_id=worker_account_id)
        return RecoveryAction(worker_account_id, "relogin_ok")

    # --- reserve promotion: rebuild the follow graph FROM THE DATABASE ---

    def promote_reserve(self, shard_id: int) -> int | None:
        """Move a `reserve` account into `shard_id` and enqueue its follow graph.

        The graph is rebuilt purely from `targets`: every active target assigned to
        this shard gets a `target_follows` row in state 'queued' for the new account.
        The follower then re-follows them at the safe rate. Nothing about the lost
        account's session, device, or proxy is reused - the reserve keeps its own
        (SPEC section 8).
        """
        now = dt.datetime.now(dt.UTC)
        with session_scope() as session:
            reserve_id = session.scalar(
                select(WorkerAccount.id)
                .where(WorkerAccount.status == "reserve")
                .order_by(WorkerAccount.id)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            if reserve_id is None:
                Alert(
                    kind="no_reserve_available",
                    severity="critical",
                    message="Account lost but no reserve account is available to promote",
                    context={"shard_id": shard_id},
                ).emit()
                return None

            session.execute(
                update(WorkerAccount)
                .where(WorkerAccount.id == reserve_id)
                .values(shard_id=shard_id, status="active", last_error=None)
            )

            # Every active target in the shard. ON CONFLICT DO NOTHING keeps
            # promotion idempotent, so re-running it after a crash is harmless.
            target_ids = [
                int(uid)
                for uid in session.scalars(
                    select(Target.user_id).where(
                        Target.shard_id == shard_id, Target.status == "active"
                    )
                ).all()
            ]
            enqueued = 0
            if target_ids:
                stmt = (
                    pg_insert(TargetFollow)
                    .values(
                        [
                            {
                                "worker_account_id": reserve_id,
                                "target_user_id": uid,
                                "state": "queued",
                                "attempts": 0,
                            }
                            for uid in target_ids
                        ]
                    )
                    .on_conflict_do_nothing(
                        index_elements=["worker_account_id", "target_user_id"]
                    )
                )
                result = session.execute(stmt)
                enqueued = int(result.rowcount or 0)

            session.add(
                AccountEvent(
                    worker_account_id=int(reserve_id),
                    event_type="recovered",
                    detail=(
                        f"promoted into shard {shard_id} at {now.isoformat()}; "
                        f"{enqueued} target_follows rows enqueued from targets table"
                    ),
                )
            )

        record_metric("reserves_promoted", 1, {"shard_id": shard_id})
        log.warning(
            "reserve_promoted",
            alert=True,
            shard_id=shard_id,
            worker_account_id=int(reserve_id),
            follows_enqueued=enqueued,
        )
        return int(reserve_id)

    # --- health checks (SPEC 7.8 + section 10) ---

    def check_health(self) -> list[Alert]:
        """All alerting conditions in one testable, reusable place."""
        alerts: list[Alert] = []
        alerts.extend(self._check_shard_coverage())
        alerts.extend(self._check_poller_lag())
        alerts.extend(self._check_queue_depth())
        alerts.extend(self._check_spend())
        return alerts

    def _check_shard_coverage(self) -> list[Alert]:
        """Alert when any shard drops below min_active_workers_per_shard."""
        minimum = self.settings.min_active_workers_per_shard
        with session_scope() as session:
            all_shards = {
                int(s)
                for s in session.scalars(
                    select(WorkerAccount.shard_id).distinct().where(
                        WorkerAccount.status.notin_(("reserve",))
                    )
                ).all()
                if s is not None
            }
            active = dict(
                session.execute(
                    select(WorkerAccount.shard_id, func.count(WorkerAccount.id))
                    .where(WorkerAccount.status == "active")
                    .group_by(WorkerAccount.shard_id)
                ).all()
            )

        alerts: list[Alert] = []
        for shard_id in sorted(all_shards):
            count = int(active.get(shard_id, 0))
            record_metric("shard_active_workers", count, {"shard_id": shard_id})
            if count < minimum:
                alerts.append(
                    Alert(
                        kind="shard_below_minimum",
                        severity="critical" if count == 0 else "warning",
                        message=f"Shard {shard_id} has {count} active workers (minimum {minimum})",
                        context={
                            "shard_id": shard_id,
                            "active_workers": count,
                            "minimum": minimum,
                        },
                    )
                )
        return alerts

    def _check_poller_lag(self) -> list[Alert]:
        """A stale last_poll_at means missed stories - they expire in 24h and are
        gone forever (SPEC section 2)."""
        threshold = self.settings.poller_lag_alert_sec
        now = dt.datetime.now(dt.UTC)
        with session_scope() as session:
            rows = session.execute(
                select(WorkerAccount.id, WorkerAccount.shard_id, WorkerAccount.last_poll_at)
                .where(WorkerAccount.status == "active")
            ).all()

        alerts: list[Alert] = []
        for worker_account_id, shard_id, last_poll_at in rows:
            if last_poll_at is None:
                lag = None
            else:
                if last_poll_at.tzinfo is None:
                    last_poll_at = last_poll_at.replace(tzinfo=dt.UTC)
                lag = (now - last_poll_at).total_seconds()
                record_metric("poller_lag_sec", lag, {"worker_account_id": worker_account_id})
            if lag is None or lag > threshold:
                alerts.append(
                    Alert(
                        kind="poller_lag",
                        severity="critical",
                        message=(
                            f"Worker {worker_account_id} has never polled"
                            if lag is None
                            else f"Worker {worker_account_id} last polled {int(lag)}s ago "
                            f"(threshold {threshold}s)"
                        ),
                        context={
                            "worker_account_id": int(worker_account_id),
                            "shard_id": shard_id,
                            "lag_sec": None if lag is None else int(lag),
                            "threshold_sec": threshold,
                        },
                    )
                )
        return alerts

    def _check_queue_depth(self, threshold: int = QUEUE_DEPTH_ALERT_THRESHOLD) -> list[Alert]:
        alerts: list[Alert] = []
        for name in MONITORED_QUEUES:
            try:
                depth = get_queue(name).depth()
            except Exception as exc:  # noqa: BLE001 - a dead Redis is itself an alert
                alerts.append(
                    Alert(
                        kind="queue_unreachable",
                        severity="critical",
                        message=f"Queue {name} is unreachable: {exc}",
                        context={"queue": name},
                    )
                )
                continue
            record_metric("queue_depth", depth, {"queue": name})
            if depth > threshold:
                alerts.append(
                    Alert(
                        kind="queue_depth",
                        severity="warning",
                        message=f"Queue {name} depth {depth} exceeds threshold {threshold}",
                        context={"queue": name, "depth": depth, "threshold": threshold},
                    )
                )
        return alerts

    def _check_spend(self) -> list[Alert]:
        """Projected monthly spend vs budget (SPEC section 2: $650/month total).

        Projection extrapolates month-to-date AI spend across the full month.
        """
        budget = self.settings.monthly_budget_usd
        now = dt.datetime.now(dt.UTC)
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

        with session_scope() as session:
            spent = session.scalar(
                select(func.coalesce(func.sum(MetricSample.value), 0)).where(
                    MetricSample.metric == "ai_estimated_spend_usd",
                    MetricSample.recorded_at >= month_start,
                )
            )

        spent = float(spent or 0.0)
        elapsed_days = max((now - month_start).total_seconds() / 86400.0, 0.5)
        days_in_month = (
            month_start.replace(year=month_start.year + (month_start.month == 12),
                                month=(month_start.month % 12) + 1) - month_start
        ).days
        projected = spent / elapsed_days * days_in_month

        record_metric("monthly_spend_projected_usd", projected, None)
        record_metric("monthly_spend_to_date_usd", spent, None)

        if projected > budget:
            return [
                Alert(
                    kind="budget_exceeded",
                    severity="critical",
                    message=(
                        f"Projected monthly spend ${projected:.2f} exceeds "
                        f"budget ${budget:.2f} (${spent:.2f} spent so far)"
                    ),
                    context={
                        "projected_usd": round(projected, 2),
                        "spent_to_date_usd": round(spent, 2),
                        "budget_usd": budget,
                    },
                )
            ]
        return []

    # --- backfill sweep ---

    def reconcile_lost_accounts(self) -> int:
        """Find shards weakened by an account that was lost while the warden was down.

        Restart-safe recovery: a challenged account whose shard is now under-staffed
        gets a reserve promoted even if nobody was watching when it failed.
        """
        minimum = self.settings.min_active_workers_per_shard
        with session_scope() as session:
            lost_shards = {
                int(s)
                for s in session.scalars(
                    select(WorkerAccount.shard_id)
                    .distinct()
                    .where(WorkerAccount.status.in_(LOST_STATUSES))
                ).all()
                if s is not None
            }
            active = dict(
                session.execute(
                    select(WorkerAccount.shard_id, func.count(WorkerAccount.id))
                    .where(WorkerAccount.status == "active")
                    .group_by(WorkerAccount.shard_id)
                ).all()
            )

        promoted = 0
        for shard_id in sorted(lost_shards):
            if int(active.get(shard_id, 0)) >= minimum:
                continue
            if self.promote_reserve(shard_id) is not None:
                promoted += 1
        return promoted

    # --- the loop ---

    def run(self, interval_sec: float = 60.0) -> None:
        log.info("warden_starting", interval_sec=interval_sec)
        while not self._stopped:
            try:
                for alert in self.check_health():
                    alert.emit()
                self.reconcile_lost_accounts()
            except Exception as exc:  # noqa: BLE001 - the loop must never die
                log.exception("warden_tick_error", error=str(exc)[:200])

            deadline = time.monotonic() + interval_sec
            while not self._stopped and time.monotonic() < deadline:
                time.sleep(min(1.0, deadline - time.monotonic()))

    def stop(self) -> None:
        self._stopped = True


def check_health() -> list[Alert]:
    """Module-level convenience so the CLI can render alerts without a Warden loop."""
    return Warden().check_health()


def run_warden() -> None:
    """Entry point (SPEC section 6)."""
    Warden().run()


__all__ = [
    "Alert",
    "BACKOFF_MAX_SEC",
    "BACKOFF_MIN_SEC",
    "MAX_CONSECUTIVE_LOGIN_FAILURES",
    "QUEUE_DEPTH_ALERT_THRESHOLD",
    "RecoveryAction",
    "Warden",
    "check_health",
    "run_warden",
]
