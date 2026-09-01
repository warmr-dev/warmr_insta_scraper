"""Store the browser User-Agent alongside each cookie set.

`datr` is Facebook's device-identity cookie: minted once for one browser on one
machine, valid for years, and checked against the User-Agent that presents it.
Every session was replayed under one hardcoded UA (Chrome 120 on macOS), so a
`datr` exported from any other browser arrived contradicting its own request -
and all accounts looked like a single machine running N sessions.

Nullable on purpose: existing rows keep working under the shared default until
someone re-pastes their cookies, and the transport warns when it falls back.

Revision ID: 0005
Revises: 0004
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("cookies", sa.Column("user_agent", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("cookies", "user_agent")
