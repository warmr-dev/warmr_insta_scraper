"""RLS: база закрыта по умолчанию.

`cookies` хранит живые сессии Instagram. Утёкший anon-ключ Supabase не должен
давать к ним доступ - поэтому RLS включён на всех таблицах и политик нет.

Проверяется структура, а не данные: тесты идут против локальной базы, где роли
`anon` может не быть.
"""

from __future__ import annotations

import pathlib
import re

MIGRATION = (
    pathlib.Path(__file__).resolve().parents[1]
    / "migrations/versions/0003_enable_rls.py"
)


def test_every_application_table_is_listed():
    """Забытая таблица - открытая таблица."""
    source = MIGRATION.read_text()
    for table in (
        "cookies",
        "stories",
        "story_analysis",
        "worker_accounts",
        "targets",
        "target_follows",
        "business_checks",
        "slack_deliveries",
        "account_events",
        "daily_action_counters",
        "metric_samples",
    ):
        assert f'"{table}"' in source, f"таблица не защищена RLS: {table}"


def test_rls_is_enabled_and_privileges_revoked():
    """Двойная защита: RLS без политик + отзыв прав у anon."""
    source = MIGRATION.read_text()
    assert "ENABLE ROW LEVEL SECURITY" in source
    assert "REVOKE ALL" in source
    assert "anon, authenticated" in source


def test_no_permissive_policy_is_created():
    """Политика USING (true) свела бы всю защиту на ноль."""
    source = MIGRATION.read_text()
    assert "CREATE POLICY" not in source, (
        "политик быть не должно: RLS без политик = запрет всем. "
        "Дашборд читает через серверные роуты с service_role."
    )
    assert not re.search(r"USING\s*\(\s*true\s*\)", source, re.I)


def test_migration_chain_is_intact():
    """Обрыв цепочки ревизий ронял контейнер на Railway."""
    source = MIGRATION.read_text()
    assert 'revision: str = "0003"' in source
    assert 'down_revision: str | None = "0002"' in source
