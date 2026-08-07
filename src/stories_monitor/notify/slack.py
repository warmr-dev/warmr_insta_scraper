"""Slack client abstraction (SPEC 7.6).

Two implementations behind one protocol: `LiveSlackNotifier` posts via
chat.postMessage, `StdoutSlackNotifier` prints the same content for fixture mode
(SPEC section 4: "Slack in fixture mode writes to stdout").

The bot token is read from settings and passed only in the Authorization header.
It is never logged - `logging_setup` redacts `slack_bot_token`, and nothing here
puts the token into an event dict.
"""

from __future__ import annotations

import datetime as dt
import sys
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

import httpx

from ..config import get_settings
from ..logging_setup import get_logger

log = get_logger(__name__)

SLACK_POST_MESSAGE_URL = "https://slack.com/api/chat.postMessage"


class SlackError(Exception):
    """Slack rejected the post, or the transport failed. Retryable by the caller."""


@dataclass(slots=True)
class LeadMessage:
    """The fields SPEC 7.6 requires in a lead notification."""

    story_id: str
    username: str | None = None
    instagram_url: str | None = None
    service_category: str | None = None
    final_score: int | None = None
    ai_explanation: str | None = None
    # Story publish time - `stories.taken_at`, not our discovery time.
    taken_at: dt.datetime | None = None
    # Business check summary: vendor, fit, geo, community (SPEC 7.5 outputs).
    vendor_id: str | None = None
    service_fit: float | None = None
    geo_ok: bool | None = None
    community_conflict: bool | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    # --- rendering helpers ---

    @property
    def display_username(self) -> str:
        return f"@{self.username}" if self.username else "(unknown account)"

    @property
    def display_url(self) -> str:
        if self.instagram_url:
            return self.instagram_url
        if self.username:
            return f"https://www.instagram.com/{self.username}/"
        return "(no url)"

    @property
    def display_taken_at(self) -> str:
        if self.taken_at is None:
            return "unknown"
        return self.taken_at.astimezone(dt.UTC).strftime("%Y-%m-%d %H:%M UTC")

    def business_summary(self) -> str:
        """One-line summary of what the check chain concluded."""
        parts = [f"vendor {self.vendor_id or 'n/a'}"]
        if self.service_fit is not None:
            parts.append(f"service fit {self.service_fit:.0f}")
        if self.geo_ok is not None:
            parts.append("geo ok" if self.geo_ok else "geo not serviceable")
        if self.community_conflict is not None:
            parts.append(
                "community conflict" if self.community_conflict else "no community conflict"
            )
        return ", ".join(parts)

    def to_text(self) -> str:
        """Readable plain text - the stdout path, and the Slack notification fallback."""
        lines = [
            "New qualified lead",
            f"  Account:          {self.display_username}",
            f"  Instagram:        {self.display_url}",
            f"  Service category: {self.service_category or 'unknown'}",
            f"  Final score:      {self.final_score if self.final_score is not None else 'n/a'}/10",
            f"  Story published:  {self.display_taken_at}",
            f"  AI explanation:   {self.ai_explanation or '(none)'}",
            f"  Business checks:  {self.business_summary()}",
            f"  Story id:         {self.story_id}",
        ]
        return "\n".join(lines)

    def to_blocks(self) -> list[dict[str, Any]]:
        """Slack Block Kit payload for the live path."""
        score = self.final_score if self.final_score is not None else "n/a"
        return [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": "New qualified lead"},
            },
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"*Account*\n{self.display_username}"},
                    {
                        "type": "mrkdwn",
                        "text": f"*Service category*\n{self.service_category or 'unknown'}",
                    },
                    {"type": "mrkdwn", "text": f"*Final score*\n{score}/10"},
                    {"type": "mrkdwn", "text": f"*Story published*\n{self.display_taken_at}"},
                ],
            },
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*Instagram*\n{self.display_url}"},
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*AI explanation*\n{self.ai_explanation or '_none_'}",
                },
            },
            {
                "type": "context",
                "elements": [
                    {"type": "mrkdwn", "text": f"Business checks: {self.business_summary()}"},
                    {"type": "mrkdwn", "text": f"story_id: `{self.story_id}`"},
                ],
            },
        ]


@runtime_checkable
class SlackNotifier(Protocol):
    """Send a lead message; return the Slack message timestamp (`slack_ts`)."""

    def send(self, message: LeadMessage) -> str: ...


class LiveSlackNotifier:
    """Posts to chat.postMessage with httpx."""

    def __init__(
        self,
        token: str | None = None,
        channel: str | None = None,
        timeout: float = 10.0,
        client: httpx.Client | None = None,
    ) -> None:
        settings = get_settings()
        self._token = token if token is not None else settings.slack_bot_token
        self._channel = channel if channel is not None else settings.slack_channel
        self._timeout = timeout
        self._client = client

    def _http(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=self._timeout)
        return self._client

    def send(self, message: LeadMessage) -> str:
        if not self._token:
            raise SlackError("slack_bot_token is not configured")

        payload = {
            "channel": self._channel,
            # `text` is the notification/fallback string; blocks are the rendering.
            "text": message.to_text(),
            "blocks": message.to_blocks(),
        }
        try:
            response = self._http().post(
                SLACK_POST_MESSAGE_URL,
                json=payload,
                # Token lives here only. Never put it in a log event.
                headers={"Authorization": f"Bearer {self._token}"},
            )
        except httpx.HTTPError as exc:
            raise SlackError(f"slack request failed: {exc}") from exc

        if response.status_code != 200:
            raise SlackError(f"slack http {response.status_code}")

        body = response.json()
        if not body.get("ok"):
            raise SlackError(f"slack error: {body.get('error', 'unknown')}")

        slack_ts = body.get("ts") or ""
        log.info(
            "slack_posted",
            story_id=message.story_id,
            channel=self._channel,
            slack_ts=slack_ts,
        )
        return slack_ts

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None


class StdoutSlackNotifier:
    """Fixture-mode notifier - writes the formatted message to stdout (SPEC section 4).

    Returns a synthetic `slack_ts` so the delivery row looks the same shape as a
    live one and the idempotency path is exercised identically.
    """

    def __init__(self, stream: Any | None = None, channel: str | None = None) -> None:
        self._stream = stream if stream is not None else sys.stdout
        self._channel = channel if channel is not None else get_settings().slack_channel

    def send(self, message: LeadMessage) -> str:
        rendered = message.to_text()
        self._stream.write(
            f"\n=== SLACK ({self._channel}) [fixture mode] ===\n{rendered}\n"
            f"{'=' * 44}\n"
        )
        self._stream.flush()
        slack_ts = f"fixture-{dt.datetime.now(dt.UTC).timestamp():.6f}"
        log.info("slack_stdout", story_id=message.story_id, slack_ts=slack_ts)
        return slack_ts


def get_notifier() -> SlackNotifier:
    """Stdout in fixture mode, real Slack otherwise (SPEC section 4)."""
    if get_settings().is_fixture_mode:
        return StdoutSlackNotifier()
    return LiveSlackNotifier()
