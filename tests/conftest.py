"""Shared pytest fixtures for the Instagram Stories Monitor test suite.

Everything here runs in fixture mode (SPEC section 4): no Instagram network I/O,
no Redis, no Anthropic key, no tesseract binary. The database is real Postgres,
because the dedup, watermark and idempotency guarantees the SPEC acceptance
criteria are about are all enforced by Postgres constraints (ON CONFLICT, primary
keys) and cannot be honestly tested against SQLite.

If Postgres is unreachable the DB-backed tests skip with a clear message rather
than erroring the whole suite.
"""

from __future__ import annotations

import datetime as dt
import os
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC = REPO_ROOT / "src"
if str(SRC) not in sys.path:  # editable install normally handles this
    sys.path.insert(0, str(SRC))

# --- environment: force fixture mode BEFORE settings are ever constructed -----

os.environ.setdefault("SECRET_KEY", "hFI-gdoATKyRad3ozn8ECE0PFA6L-dPmKMP5HXALhRY=")
os.environ["IG_TRANSPORT"] = "fixture"
os.environ["FIXTURES_DIR"] = str(REPO_ROOT / "fixtures")
# No Anthropic key -> FakeAIClient; no tesseract -> the analyzer's NullOCR path.
os.environ["ANTHROPIC_API_KEY"] = ""
os.environ["SLACK_BOT_TOKEN"] = ""
# Keep worker batching snappy so tests never block on a 5s queue timeout.
os.environ.setdefault("FETCH_BATCH_TIMEOUT_SEC", "0")

_DEFAULT_TEST_DB = "postgresql+psycopg://{user}@localhost:5432/stories_monitor_test"


def _base_database_url() -> str:
    """The DATABASE_URL from .env (or the environment), minus the database name."""
    from stories_monitor.config import Settings

    url = Settings().database_url
    return url


def _test_database_url() -> str:
    """Point at a dedicated, PER-PROCESS `<db>_test_<pid>` database.

    Two things matter here. First, never touch the dev database. Second, never
    share a test database between concurrent pytest processes: the per-test
    TRUNCATE below would wipe another run's rows mid-test, producing failures
    that move around between runs and look like flaky implementation bugs. The
    pid suffix makes concurrent runs fully independent.
    """
    explicit = os.environ.get("TEST_DATABASE_URL")
    if explicit:
        return explicit
    base = _base_database_url()
    head, _, tail = base.rpartition("/")
    dbname, sep, query = tail.partition("?")
    return f"{head}/{dbname}_test_{os.getpid()}{sep}{query}"


def _ensure_test_database(url: str) -> None:
    """CREATE DATABASE the test db if it is absent. Raises on an unreachable server."""
    import sqlalchemy as sa

    head, _, tail = url.rpartition("/")
    dbname = tail.partition("?")[0]
    admin_url = f"{head}/postgres"

    engine = sa.create_engine(admin_url, isolation_level="AUTOCOMMIT", future=True)
    try:
        with engine.connect() as conn:
            exists = conn.execute(
                sa.text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": dbname}
            ).scalar()
            if not exists:
                conn.execute(sa.text(f'CREATE DATABASE "{dbname}"'))
    finally:
        engine.dispose()


def _drop_test_database(url: str) -> None:
    """Drop the per-process test database. Best effort - never fails a run."""
    import sqlalchemy as sa

    head, _, tail = url.rpartition("/")
    dbname = tail.partition("?")[0]
    engine = sa.create_engine(f"{head}/postgres", isolation_level="AUTOCOMMIT", future=True)
    try:
        with engine.connect() as conn:
            conn.execute(sa.text(f'DROP DATABASE IF EXISTS "{dbname}" WITH (FORCE)'))
    except Exception:  # noqa: BLE001 - cleanup must never fail the suite
        pass
    finally:
        engine.dispose()


@pytest.fixture(scope="session")
def database_url():
    """Session-scoped: create the test database and build the schema once.

    Skips (rather than errors) when Postgres is not reachable.
    """
    url = _test_database_url()
    try:
        _ensure_test_database(url)
    except Exception as exc:  # noqa: BLE001
        pytest.skip(
            f"Postgres is not reachable at {url!r} ({type(exc).__name__}: {exc}). "
            "Start Postgres, or set TEST_DATABASE_URL, to run the database tests."
        )

    os.environ["DATABASE_URL"] = url

    from stories_monitor.config import get_settings
    from stories_monitor.db import session as db_session
    from stories_monitor.db.models import Base

    get_settings.cache_clear()
    db_session.reset_engine()

    engine = db_session.get_engine()
    # Full schema straight from the models - the same metadata Alembic generates.
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)

    yield url

    # Tear the per-process database down so runs do not litter the server.
    if not os.environ.get("TEST_DATABASE_URL"):
        db_session.reset_engine()
        _drop_test_database(url)


@pytest.fixture()
def db(database_url: str):
    """Per-test clean slate: truncate every table, then hand back the sessionmaker.

    TRUNCATE is used rather than an outer transaction because several code paths
    under test open their own `session_scope()` and commit - a wrapping
    transaction would hide exactly the commit behaviour we are asserting on.
    """
    import sqlalchemy as sa

    from stories_monitor.config import get_settings
    from stories_monitor.db import session as db_session
    from stories_monitor.db.models import Base

    get_settings.cache_clear()
    db_session.reset_engine()
    engine = db_session.get_engine()

    tables = ", ".join(f'"{t.name}"' for t in reversed(Base.metadata.sorted_tables))
    with engine.begin() as conn:
        conn.execute(sa.text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))

    yield db_session.get_sessionmaker()

    with engine.begin() as conn:
        conn.execute(sa.text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))


@pytest.fixture()
def settings(db):
    """Fresh Settings with the lru_cache cleared. Depends on `db` for ordering."""
    from stories_monitor.config import get_settings

    get_settings.cache_clear()
    return get_settings()


@pytest.fixture(autouse=True)
def _reset_module_singletons():
    """Drop cached queues / AI client / vendor repo between tests.

    Every one of these is a process-wide singleton; leaking one across tests makes
    a queue-depth or call-count assertion depend on test ordering.
    """
    from stories_monitor.ai.client import reset_ai_client
    from stories_monitor.queue import reset_queues
    from stories_monitor.vendors.stub import reset_vendor_repository

    reset_queues()
    reset_ai_client()
    reset_vendor_repository()
    yield
    reset_queues()
    reset_ai_client()
    reset_vendor_repository()


# --- queues -------------------------------------------------------------------


@pytest.fixture()
def fake_queues():
    """Force every named queue to an in-memory FakeQueue (no Redis in this env).

    The instances are seeded into `stories_monitor.queue._queues`, which is the
    cache `get_queue(name)` reads, so a worker constructing its own queues picks
    up exactly these objects. Returns a dict name -> FakeQueue so tests can
    assert on `depth()` and drain contents directly.
    """
    from stories_monitor import queue as queue_mod
    from stories_monitor.config import (
        Q_ANALYZE,
        Q_BIZCHECK,
        Q_FETCH,
        Q_FETCH_DIRECT,
        Q_NOTIFY,
    )

    queue_mod.reset_queues()
    made: dict[str, Any] = {}
    for name in (Q_FETCH, Q_FETCH_DIRECT, Q_ANALYZE, Q_BIZCHECK, Q_NOTIFY):
        made[name] = queue_mod.FakeQueue(name)
    with queue_mod._queues_lock:
        queue_mod._queues.update(made)
    yield made
    queue_mod.reset_queues()


def drain(queue) -> list[dict[str, Any]]:
    """Pop everything currently on a queue, without blocking. Assertion helper."""
    items: list[dict[str, Any]] = []
    while (item := queue.pop_blocking(timeout=0)) is not None:
        items.append(item)
    return items


# --- factories ----------------------------------------------------------------


@pytest.fixture()
def secret_box(settings):
    from stories_monitor.crypto import SecretBox

    return SecretBox()


@pytest.fixture()
def make_worker_account(db, secret_box):
    """Create a WorkerAccount with an encrypted password, device_settings and proxy."""
    from stories_monitor.db.models import WorkerAccount

    counter = {"n": 0}

    def _make(
        *,
        username: str | None = None,
        shard_id: int = 1,
        status: str = "active",
        password: str = "hunter2-not-a-real-password",
        proxy_url: str | None = None,
        device_settings: dict[str, Any] | None = None,
        session_json: dict[str, Any] | None = None,
        phase_offset_sec: int = 0,
        last_poll_at: dt.datetime | None = None,
        follows_count: int = 0,
    ) -> int:
        counter["n"] += 1
        n = counter["n"]
        with db() as session:
            account = WorkerAccount(
                username=username or f"worker_{n}",
                password_enc=secret_box.encrypt(password),
                shard_id=shard_id,
                proxy_url=proxy_url or f"http://user{n}:pw@proxy{n}.example:8080",
                device_settings=device_settings
                or {
                    "app_version": "269.0.0.18.75",
                    "android_version": 26,
                    "manufacturer": "Xiaomi",
                    "model": "MI 5s Plus",
                    # UTC so the follow-window tests can reason about local time.
                    "timezone_offset": 0,
                },
                session_json=session_json if session_json is not None else {"cookies": {}},
                status=status,
                phase_offset_sec=phase_offset_sec,
                follows_count=follows_count,
                last_poll_at=last_poll_at,
            )
            session.add(account)
            session.commit()
            return int(account.id)

    return _make


@pytest.fixture()
def make_target(db):
    """Create a Target row. Defaults line up with the committed fixtures."""
    from stories_monitor.db.models import Target

    def _make(
        user_id: int,
        *,
        username: str | None = None,
        shard_id: int = 1,
        status: str = "active",
        last_reel_media_ts: int | None = None,
    ) -> int:
        with db() as session:
            session.add(
                Target(
                    user_id=user_id,
                    username=username or f"user_{user_id}",
                    instagram_url=f"https://www.instagram.com/{username or f'user_{user_id}'}/",
                    shard_id=shard_id,
                    status=status,
                    last_reel_media_ts=last_reel_media_ts,
                )
            )
            session.commit()
        return user_id

    return _make


# The three user ids used across every committed fixture (fixtures/README.md).
FIXTURE_USER_IDS = (1234567890, 2345678901, 3456789012)


@pytest.fixture()
def fixture_targets(make_target):
    """The three fixture accounts, present in `targets` with no watermark yet."""
    usernames = {
        1234567890: "acme_dental",
        2345678901: "north_cafe",
        3456789012: "vega_fitness",
    }
    for user_id in FIXTURE_USER_IDS:
        make_target(user_id, username=usernames[user_id])
    return list(FIXTURE_USER_IDS)


@pytest.fixture()
def make_story(db):
    """Create a Story row (and optionally its analysis / business check)."""
    from stories_monitor.db.models import BusinessCheck, Story, StoryAnalysis

    def _make(
        story_id: str,
        target_user_id: int,
        *,
        media_type: int = 1,
        pipeline_state: str = "new",
        taken_at: dt.datetime | None = None,
        analysis: dict[str, Any] | None = None,
        business_check: dict[str, Any] | None = None,
    ) -> str:
        with db() as session:
            session.add(
                Story(
                    story_id=story_id,
                    target_user_id=target_user_id,
                    taken_at=taken_at or dt.datetime.now(dt.UTC),
                    expiring_at=(taken_at or dt.datetime.now(dt.UTC))
                    + dt.timedelta(hours=24),
                    media_type=media_type,
                    pipeline_state=pipeline_state,
                )
            )
            if analysis is not None:
                session.add(StoryAnalysis(story_id=story_id, **analysis))
            if business_check is not None:
                session.add(BusinessCheck(story_id=story_id, **business_check))
            session.commit()
        return story_id

    return _make


@pytest.fixture()
def make_target_follow(db):
    from stories_monitor.db.models import TargetFollow

    def _make(
        worker_account_id: int,
        target_user_id: int,
        *,
        state: str = "queued",
        attempts: int = 0,
        requested_at: dt.datetime | None = None,
    ) -> None:
        with db() as session:
            session.add(
                TargetFollow(
                    worker_account_id=worker_account_id,
                    target_user_id=target_user_id,
                    state=state,
                    attempts=attempts,
                    requested_at=requested_at,
                )
            )
            session.commit()

    return _make


@pytest.fixture()
def fixture_transport():
    """A FixtureTransport bound to the committed fixtures directory."""
    from stories_monitor.transport.fixture import FixtureTransport

    def _make(**kwargs: Any) -> FixtureTransport:
        kwargs.setdefault("fixtures_dir", REPO_ROOT / "fixtures")
        return FixtureTransport(**kwargs)

    return _make


@pytest.fixture()
def fake_ai():
    """The deterministic FakeAIClient with call counters (no Anthropic key needed)."""
    from stories_monitor.ai.client import FakeAIClient

    return FakeAIClient()


@pytest.fixture()
def null_ocr():
    """NullOCR - tesseract is not installed in this environment."""
    from stories_monitor.ai.ocr import NullOCR

    return NullOCR()
