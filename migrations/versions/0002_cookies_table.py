"""Таблица cookies - веб-куки по одному аккаунту на строку

Отдельная таблица, а не JSONB в worker_accounts: её удобно править руками
в интерфейсе Supabase, когда куки истекли и их надо обновить. Это не
теоретическое удобство - веб-куки нельзя продлить из кода, поэтому ручное
обновление входит в штатную эксплуатацию.

Revision ID: 0002
Revises: 0001
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "cookies",
        # Чьи это куки. Ключ, потому что у аккаунта один активный набор.
        sa.Column("username", sa.Text(), nullable=False),
        # Семь куки, которые нужны веб-API. Без ig_did/mid/datr/rur ленты
        # отвечают 302 - проверено.
        sa.Column("sessionid", sa.Text(), nullable=False),
        sa.Column("csrftoken", sa.Text(), nullable=True),
        sa.Column("ds_user_id", sa.Text(), nullable=True),
        sa.Column("ig_did", sa.Text(), nullable=True),
        sa.Column("mid", sa.Text(), nullable=True),
        sa.Column("datr", sa.Text(), nullable=True),
        sa.Column("rur", sa.Text(), nullable=True),
        # Выключить аккаунт, не удаляя куки.
        sa.Column(
            "is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")
        ),
        # Когда обновляли - по этому полю видно, какие куки протухают.
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("username", name="pk_cookies"),
    )
    op.create_index("ix_cookies_active", "cookies", ["is_active"])


def downgrade() -> None:
    op.drop_index("ix_cookies_active", table_name="cookies")
    op.drop_table("cookies")
