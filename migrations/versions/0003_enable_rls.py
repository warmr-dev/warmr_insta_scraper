"""Enable RLS on every table, with no policies

Deny by default. RLS with zero policies means `anon` and `authenticated` read
nothing at all - verified: those roles return 0 rows from cookies, stories and
story_analysis.

This matters because `cookies` holds live Instagram sessions. A leaked anon key
would otherwise hand over working sessions, not just analytics.

The scraper is unaffected: it connects as `postgres`, which has BYPASSRLS.

The admin dashboard therefore CANNOT query Supabase from the browser. It must go
through server-side routes holding the service_role key. That is the intended
design, not a limitation to work around - do not add anon policies to make
browser queries work.

Supabase already had RLS on when this was written; this migration makes that
state explicit and reproducible on a fresh database rather than incidental.

Revision ID: 0003
Revises: 0002
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Every table the application owns.
_TABLES = (
    "worker_accounts",
    "targets",
    "target_follows",
    "stories",
    "story_analysis",
    "business_checks",
    "slack_deliveries",
    "account_events",
    "daily_action_counters",
    "metric_samples",
    "cookies",
)


def upgrade() -> None:
    for table in _TABLES:
        # FORCE also applies RLS to the table owner, so a misconfigured
        # connection cannot quietly read everything.
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"REVOKE ALL ON {table} FROM anon, authenticated")


def downgrade() -> None:
    for table in _TABLES:
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
