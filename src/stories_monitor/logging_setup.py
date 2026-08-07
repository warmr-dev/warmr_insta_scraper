"""Structured JSON logging. Never log passwords or session cookies."""

from __future__ import annotations

import logging
import sys

import structlog

# Keys whose values must never reach a log line.
_REDACT_KEYS = {
    "password",
    "password_enc",
    "session_json",
    "sessionid",
    "authorization",
    "cookie",
    "cookies",
    "secret_key",
    "anthropic_api_key",
    "slack_bot_token",
    "proxy_url",
    "csrftoken",
    "ds_user_id",
}


def _redact(_logger, _name, event_dict):
    for key in list(event_dict):
        if key.lower() in _REDACT_KEYS:
            event_dict[key] = "***REDACTED***"
    return event_dict


def configure_logging(level: str = "INFO", json_output: bool = True) -> None:
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level)

    renderer = (
        structlog.processors.JSONRenderer()
        if json_output
        else structlog.dev.ConsoleRenderer()
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            _redact,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper(), logging.INFO)
        ),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str):
    return structlog.get_logger(name)
