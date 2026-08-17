"""Configuration. All secrets come from the environment - never hardcoded."""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


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
    # Redis нужен только распределённому мобильному пути (ТЗ §6). Веб-путь
    # последовательный, очереди держит в памяти. По умолчанию выключен:
    # попытка подключиться к отсутствующему серверу роняла контейнер.
    use_redis: bool = False
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
    # TEMPORARY: `gemini` is in use because only a Gemini key is available.
    # The pipeline is provider-agnostic behind the AIClient protocol, so moving
    # back is a config change, not a rewrite.
    ai_provider: Literal["anthropic", "gemini", "openrouter"] = "anthropic"
    anthropic_api_key: str = ""
    gemini_api_key: str = ""
    # OpenRouter: один ключ, любые модели. ВАЖНО: id модели обязан содержать
    # префикс провайдера ("google/gemini-2.5-flash-lite"), иначе 404.
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

    # --- Приоритизация целей (ТЗ §5: неактивных проверять реже) ---
    priority_enabled: bool = True
    # Сколько фото должно пройти без сигнала, прежде чем считать цель пустой.
    priority_min_samples: int = 8
    # Даже "пустая" цель получает шанс раз в N часов - люди меняют поведение.
    priority_recheck_hours: int = 24

    # --- Telegram (алерты о простое; лиды идут в Slack) ---
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    # Одно и то же состояние не шлём чаще - иначе цикл раз в минуту спамит.
    alert_cooldown_sec: int = 3600
    # Дублировать найденные лиды в Telegram, а не только в Slack.
    telegram_send_leads: bool = True

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

    @property
    def is_fixture_mode(self) -> bool:
        return self.ig_transport == "fixture"

    @property
    def ai_api_key(self) -> str:
        """API key for whichever provider is active. Empty means use the fake."""
        return {
            "gemini": self.gemini_api_key,
            "openrouter": self.openrouter_api_key,
        }.get(self.ai_provider, self.anthropic_api_key)


@lru_cache
def get_settings() -> Settings:
    return Settings()


# Queue names (Redis lists)
Q_FETCH = "q:fetch"
Q_FETCH_DIRECT = "q:fetch_direct"
Q_ANALYZE = "q:analyze"
Q_BIZCHECK = "q:bizcheck"
Q_NOTIFY = "q:notify"
