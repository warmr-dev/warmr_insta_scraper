"""Configuration. All secrets come from the environment - never hardcoded."""

from __future__ import annotations

import os
from functools import lru_cache
from typing import Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def normalize_database_url(url: str) -> str:
    """Convert Railway/Heroku postgres URLs to the psycopg3 SQLAlchemy dialect."""
    url = url.strip()
    if url.startswith("postgres://"):
        return "postgresql+psycopg://" + url.removeprefix("postgres://")
    if url.startswith("postgresql://"):
        return "postgresql+psycopg://" + url.removeprefix("postgresql://")
    return url


def _is_postgres_url(url: str) -> bool:
    lowered = url.strip().lower()
    return lowered.startswith(("postgres://", "postgresql://", "postgresql+psycopg://"))


def resolve_database_url_from_env(*, skip: frozenset[str] = frozenset()) -> str:
    """Pick the first usable Postgres URL from common Railway/Heroku env vars."""
    for key in ("DATABASE_URL", "DATABASE_PRIVATE_URL", "DATABASE_PUBLIC_URL"):
        if key in skip:
            continue
        raw = os.environ.get(key, "").strip()
        if raw and _is_postgres_url(raw):
            return normalize_database_url(raw)

    host = os.environ.get("PGHOST", "").strip()
    if not host:
        return ""

    user = os.environ.get("PGUSER", "postgres")
    password = os.environ.get("PGPASSWORD", "")
    port = os.environ.get("PGPORT", "5432")
    database = os.environ.get("PGDATABASE", "railway")
    creds = f"{user}:{password}@" if password else f"{user}@"
    return normalize_database_url(f"postgresql://{creds}{host}:{port}/{database}")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # --- Transport ---
    ig_transport: Literal["fixture", "live"] = "fixture"
    fixtures_dir: str = "fixtures"

    # --- Secrets ---
    secret_key: str = "REPLACE_WITH_FERNET_KEY"

    # --- Infra ---
    database_url: str = "postgresql+psycopg://postgres:postgres@localhost:5432/stories_monitor"
    redis_url: str = "redis://localhost:6379/0"

    # --- Poller ---
    poll_interval_sec: int = 120
    poll_jitter_max_sec: int = 30
    tray_pagination_enabled: bool = False
    tray_page_size: int = 50

    # --- Fetcher ---
    fetch_batch_size: int = 50
    fetch_batch_timeout_sec: int = 5
    media_tmp_dir: str = "tmp_media"

    # --- AI ---
    # TEMPORARY: `gemini` / `openrouter` are in use when only those keys are available.
    # The pipeline is provider-agnostic behind the AIClient protocol, so moving
    # back is a config change, not a rewrite.
    ai_provider: Literal["anthropic", "gemini", "openrouter"] = "anthropic"
    anthropic_api_key: str = ""
    gemini_api_key: str = ""
    openrouter_api_key: str = ""
    cheap_model: str = "claude-haiku-4-5-20251001"
    smart_model: str = "claude-sonnet-5"
    ocr_engine: Literal["tesseract", "vision"] = "tesseract"
    smart_model_score_min: int = 5
    smart_model_score_max: int = 6

    # --- Business checks ---
    # SPEC open question #4: spec says "roughly 70-75". Configurable pending confirmation.
    service_fit_threshold: float = 70.0
    approval_score_min: int = 7

    # --- Slack ---
    slack_bot_token: str = ""
    slack_channel: str = "#leads"
    slack_max_attempts: int = 5

    # --- Follower ---
    follows_per_day: int = 150
    follow_gap_min_sec: int = 240
    follow_gap_max_sec: int = 720
    follow_window_start_hour: int = 9
    follow_window_end_hour: int = 23

    # --- Warden ---
    min_active_workers_per_shard: int = 2
    poller_lag_alert_sec: int = 300
    monthly_budget_usd: float = 650.0

    # --- Worker seeding ---
    # Source values for `stories seed-worker`. Once seeded, the database is the
    # authority: the password lives encrypted in worker_accounts and these are
    # no longer read.
    ig_worker_username: str = ""
    ig_worker_password: str = ""
    ig_worker_proxy_url: str = ""
    # TOTP secret ("setup key") for a 2FA account. With it, logins are automatic;
    # without it a human must supply a fresh 6-digit code for every login, which
    # the follower and warden cannot do unattended.
    ig_worker_totp_secret: str = ""

    # --- Sharding ---
    # Instagram caps following at 7,500/account. Leave headroom.
    max_follows_per_account: int = 7000

    @field_validator("database_url", mode="before")
    @classmethod
    def _prepare_database_url(cls, value: object) -> object:
        if isinstance(value, str) and value.strip().startswith(("http://", "https://")):
            fallback = resolve_database_url_from_env(skip=frozenset({"DATABASE_URL"}))
            if fallback:
                return fallback
            raise ValueError(
                "DATABASE_URL must be a PostgreSQL connection string (postgresql://...), "
                "not an HTTP URL. On Railway, set it to the Postgres service variable, "
                "for example: ${{Postgres.DATABASE_URL}}."
            )
        if isinstance(value, str) and value.strip() and _is_postgres_url(value):
            return normalize_database_url(value)
        if isinstance(value, str) and not value.strip():
            resolved = resolve_database_url_from_env()
            return resolved if resolved else value
        return value

    @property
    def is_fixture_mode(self) -> bool:
        return self.ig_transport == "fixture"

    @property
    def ai_api_key(self) -> str:
        """API key for whichever provider is active. Empty means use the fake."""
        match self.ai_provider:
            case "gemini":
                return self.gemini_api_key
            case "openrouter":
                return self.openrouter_api_key
            case "anthropic":
                return self.anthropic_api_key
            case unreachable:
                raise AssertionError(f"unhandled ai_provider: {unreachable!r}")


@lru_cache
def get_settings() -> Settings:
    return Settings()


# Queue names (Redis lists)
Q_FETCH = "q:fetch"
Q_FETCH_DIRECT = "q:fetch_direct"
Q_ANALYZE = "q:analyze"
Q_BIZCHECK = "q:bizcheck"
Q_NOTIFY = "q:notify"
