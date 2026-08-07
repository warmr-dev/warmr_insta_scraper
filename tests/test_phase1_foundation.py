"""SPEC Phase 1 acceptance criteria (SPEC section 9).

*Accept:* full schema migrates cleanly; CSV imports with a count report; fixture
transport returns parsed tray data in tests.

Plus the two Phase 1 correctness requirements from SPEC section 4: passwords are
encrypted at rest with a key from SECRET_KEY, and nothing ever logs a password,
session cookie, token or proxy.
"""

from __future__ import annotations

import io
import json

import pytest
import sqlalchemy as sa

from stories_monitor.crypto import SecretBox
from stories_monitor.db.models import Base, Target
from stories_monitor.importer import ImportReport, compute_shard_count, import_csv, parse_csv
from stories_monitor.transport.base import TrayEntry, TrayResponse

# --- schema -------------------------------------------------------------------

EXPECTED_TABLES = {
    "worker_accounts",
    "targets",
    "target_follows",
    "stories",
    "story_analysis",
    "business_checks",
    "slack_deliveries",
    "account_events",
    "daily_action_counters",
}

EXPECTED_INDEXES = {
    # SPEC section 5 names these two explicitly.
    "targets": "ix_targets_shard_status",
    "target_follows": "ix_target_follows_worker_state",
}


def test_full_schema_migrates_cleanly(db):
    """Every SPEC section 5 table and the two named indexes exist."""
    from stories_monitor.db.session import get_engine

    inspector = sa.inspect(get_engine())
    tables = set(inspector.get_table_names())

    missing = EXPECTED_TABLES - tables
    assert not missing, f"tables missing from the schema: {sorted(missing)}"

    for table, index_name in EXPECTED_INDEXES.items():
        names = {i["name"] for i in inspector.get_indexes(table)}
        assert index_name in names, f"{table} is missing index {index_name}; has {sorted(names)}"

    # Primary keys carry the dedup / idempotency guarantees - assert them.
    assert inspector.get_pk_constraint("stories")["constrained_columns"] == ["story_id"]
    assert inspector.get_pk_constraint("slack_deliveries")["constrained_columns"] == ["story_id"]
    assert inspector.get_pk_constraint("targets")["constrained_columns"] == ["user_id"]
    assert set(
        inspector.get_pk_constraint("daily_action_counters")["constrained_columns"]
    ) == {"worker_account_id", "day"}
    assert set(inspector.get_pk_constraint("target_follows")["constrained_columns"]) == {
        "worker_account_id",
        "target_user_id",
    }


def test_schema_matches_the_models(db):
    """The live schema and Base.metadata agree on column names (SPEC 5 is normative)."""
    from stories_monitor.db.session import get_engine

    inspector = sa.inspect(get_engine())
    for table in Base.metadata.sorted_tables:
        live = {c["name"] for c in inspector.get_columns(table.name)}
        declared = {c.name for c in table.columns}
        assert declared <= live, (
            f"{table.name}: columns declared in the model but absent in the DB: "
            f"{sorted(declared - live)}"
        )


# --- fixture transport --------------------------------------------------------


def test_fixture_transport_returns_parsed_tray_data(fixture_transport):
    """Phase 1 acceptance: the fixture transport returns parsed tray data."""
    transport = fixture_transport(scenario="normal_tray")
    tray = transport.reels_tray(cold_start=True)

    assert isinstance(tray, TrayResponse)
    assert tray.entry_count == 3
    assert all(isinstance(e, TrayEntry) for e in tray.entries)

    entry = next(e for e in tray.entries if e.id == "1234567890")
    assert entry.is_user_entry is True
    assert entry.user_id == 1234567890
    assert entry.latest_reel_media == 1786095000
    assert entry.user["username"] == "acme_dental"
    assert entry.has_prefetched_items is False

    # No network was touched - the transport just records the calls it served.
    assert transport.calls[0][0] == "reels_tray"


def test_fixture_transport_empty_tray_is_not_an_error(fixture_transport):
    tray = fixture_transport(scenario="empty_tray").reels_tray()
    assert tray.entries == []
    assert tray.entry_count == 0


def test_fixture_transport_reels_media_parses_story_items(fixture_transport):
    transport = fixture_transport(media_scenario="photo_story")
    reels = transport.reels_media([1234567890])

    assert set(reels) == {1234567890}
    items = reels[1234567890]
    assert len(items) == 2
    assert all(i.media_type == 1 for i in items)
    # Candidates are deliberately unsorted in the fixture: best is the 1080x1920.
    assert items[0].best_image_url().endswith("1080x1920.jpg")


@pytest.mark.parametrize(
    ("scenario", "exc_name"),
    [
        ("error_challenge_required", "ChallengeRequiredError"),
        ("error_login_required", "LoginRequiredError"),
        ("error_feedback_required", "FeedbackRequiredError"),
    ],
)
def test_fixture_transport_error_scenarios_raise_transport_errors(
    fixture_transport, scenario, exc_name
):
    from stories_monitor import transport as transport_pkg

    exc_type = getattr(transport_pkg, exc_name)
    with pytest.raises(exc_type):
        fixture_transport(scenario=scenario).reels_tray()


# --- CSV importer -------------------------------------------------------------


CSV_HEADER = "user_id,username,instagram_url\n"


def _write_csv(tmp_path, rows: str, name: str = "targets.csv"):
    path = tmp_path / name
    path.write_text(CSV_HEADER + rows, encoding="utf-8")
    return path


def test_csv_importer_reports_counts_normalises_and_drops_duplicates(db, tmp_path):
    """Phase 1 acceptance: imports with a count report, normalises, drops duplicates."""
    csv_path = _write_csv(
        tmp_path,
        # Deliberately messy: an @-prefixed handle, a spreadsheet float id, a
        # thousands-separated id, an exact duplicate, and a blank row.
        "1234567890,@Acme_Dental,https://instagram.com/acme_dental\n"
        '2345678901.0,north_cafe,\n'
        '"3,456,789,012",VEGA_fitness,instagram.com/vega_fitness\n'
        "1234567890,acme_dental_again,\n"
        ",,\n",
    )

    report = import_csv(csv_path, shard_count=8)

    assert isinstance(report, ImportReport)
    assert report.rows_read == 5
    assert report.valid == 3
    assert report.duplicates_dropped == 1, "the repeated user_id must be dropped"
    assert report.skipped == 1
    assert report.inserted == 3
    assert report.updated == 0
    assert report.shard_count == 8
    # The report renders as a human-readable count report.
    rendered = report.render()
    assert "duplicates dropped    : 1" in rendered
    assert "rows read             : 5" in rendered

    with db() as session:
        rows = {
            int(t.user_id): t
            for t in session.execute(sa.select(Target)).scalars().all()
        }

    assert set(rows) == {1234567890, 2345678901, 3456789012}, (
        "user_id must be normalised: floats, thousands separators and quotes stripped"
    )
    # Usernames are lower-cased and @-stripped.
    assert rows[1234567890].username == "acme_dental"
    assert rows[3456789012].username == "vega_fitness"
    # A missing URL is synthesised from the username.
    assert rows[2345678901].instagram_url == "https://www.instagram.com/north_cafe/"
    # Every row gets a shard_id inside the requested shard count.
    assert all(0 <= r.shard_id < 8 for r in rows.values())


def test_csv_importer_assigns_shard_ids_across_all_shards(db, tmp_path):
    """Sharding is balanced round-robin: 24 rows over 8 shards is 3 each."""
    rows = "".join(
        f"{1000 + i},user_{i},\n" for i in range(24)
    )
    report = import_csv(_write_csv(tmp_path, rows), shard_count=8)

    assert report.shard_count == 8
    assert set(report.per_shard) == set(range(8))
    assert set(report.per_shard.values()) == {3}

    with db() as session:
        counts = dict(
            session.execute(
                sa.select(Target.shard_id, sa.func.count()).group_by(Target.shard_id)
            ).all()
        )
    assert counts == {shard: 3 for shard in range(8)}


def test_csv_importer_is_idempotent_and_never_clobbers_the_watermark(db, tmp_path):
    """Re-running must not duplicate rows and must not touch last_reel_media_ts.

    `last_reel_media_ts` is live state owned by the poller (SPEC 7.1). An import
    that reset it would make the poller re-enqueue every story it already saw.
    """
    csv_path = _write_csv(
        tmp_path,
        "1234567890,acme_dental,\n2345678901,north_cafe,\n3456789012,vega_fitness,\n",
    )

    first = import_csv(csv_path, shard_count=8)
    assert first.inserted == 3

    # The poller advances a watermark between the two imports.
    with db() as session:
        session.execute(
            sa.update(Target)
            .where(Target.user_id == 1234567890)
            .values(last_reel_media_ts=1786095000, status="private")
        )
        session.commit()

    second = import_csv(csv_path, shard_count=8)

    assert second.inserted == 0, "re-import must not insert duplicate rows"
    assert second.updated == 3

    with db() as session:
        total = session.execute(sa.select(sa.func.count()).select_from(Target)).scalar()
        row = session.get(Target, 1234567890)

    assert total == 3, "re-running the import duplicated rows"
    assert row.last_reel_media_ts == 1786095000, (
        "the importer clobbered the poller's change-detection watermark"
    )
    assert row.status == "private", "the importer clobbered poller-owned status"


def test_csv_importer_dry_run_writes_nothing(db, tmp_path):
    csv_path = _write_csv(tmp_path, "1234567890,acme_dental,\n")
    report = import_csv(csv_path, dry_run=True)
    assert report.valid == 1
    with db() as session:
        assert session.execute(sa.select(sa.func.count()).select_from(Target)).scalar() == 0


def test_compute_shard_count_never_drops_below_eight(settings):
    """SPEC section 2: 7,500 following cap -> 8 shards minimum for 57k targets."""
    assert compute_shard_count(10) == 8
    assert compute_shard_count(0) == 8
    assert compute_shard_count(57_000, max_per_account=7000) == 9


def test_parse_csv_drops_duplicate_usernames_without_ids(tmp_path):
    path = tmp_path / "handles.csv"
    path.write_text("username\nacme_dental\n@Acme_Dental\nnorth_cafe\n", encoding="utf-8")
    report = ImportReport()
    rows = parse_csv(path, report)

    assert [r.username for r in rows] == ["acme_dental", "north_cafe"]
    assert report.duplicates_dropped == 1
    assert report.needs_resolution == 2


# --- crypto -------------------------------------------------------------------


def test_secretbox_encrypt_decrypt_round_trip(settings):
    box = SecretBox()
    plaintext = "correct horse battery staple"
    ciphertext = box.encrypt(plaintext)

    assert isinstance(ciphertext, bytes)
    assert plaintext.encode() not in ciphertext, "password must not appear in the ciphertext"
    assert box.decrypt(ciphertext) == plaintext
    # Fernet is randomised: the same plaintext encrypts differently every time.
    assert box.encrypt(plaintext) != ciphertext


def test_secretbox_rejects_an_unset_key(monkeypatch):
    """SPEC section 4: the key comes from the environment; there is no default.

    An explicit placeholder key is rejected, and so is an unconfigured
    environment - `SecretBox()` must never silently fall back to a constant.
    """
    from stories_monitor.config import get_settings

    with pytest.raises(ValueError, match="SECRET_KEY is not configured"):
        SecretBox(key="REPLACE_WITH_FERNET_KEY")

    # An empty explicit key falls through to the environment, which must itself
    # be unconfigured-safe.
    monkeypatch.setenv("SECRET_KEY", "REPLACE_WITH_FERNET_KEY")
    get_settings.cache_clear()
    try:
        with pytest.raises(ValueError, match="SECRET_KEY is not configured"):
            SecretBox()
        with pytest.raises(ValueError, match="SECRET_KEY is not configured"):
            SecretBox(key="")
    finally:
        get_settings.cache_clear()


def test_worker_account_password_is_stored_encrypted(db, make_worker_account, secret_box):
    from stories_monitor.db.models import WorkerAccount

    worker_id = make_worker_account(password="s3cr3t-pw")
    with db() as session:
        account = session.get(WorkerAccount, worker_id)
        stored = bytes(account.password_enc)

    assert b"s3cr3t-pw" not in stored
    assert secret_box.decrypt(stored) == "s3cr3t-pw"


# --- logging redaction --------------------------------------------------------


REDACTED_FIELDS = {
    "password": "hunter2-plaintext-password",
    "password_enc": "gAAAAAB-fake-ciphertext",
    "session_json": "sessionid=THE-REAL-SESSION-COOKIE",
    "sessionid": "THE-REAL-SESSION-COOKIE",
    "authorization": "Bearer xoxb-real-slack-token",
    "cookie": "csrftoken=abc123",
    "secret_key": "hFI-gdoATKyRad3ozn8ECE0PFA6L-dPmKMP5HXALhRY=",
    "anthropic_api_key": "sk-ant-real-key",
    "slack_bot_token": "xoxb-real-slack-token",
    "proxy_url": "http://user:pw@proxy1.example:8080",
    "csrftoken": "abc123",
    "ds_user_id": "9999999999",
}


def test_logging_redacts_secrets_from_rendered_output():
    """Passwords, sessions, tokens and proxies never reach a rendered log line."""
    import structlog

    from stories_monitor.logging_setup import _redact

    stream = io.StringIO()
    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            _redact,
            structlog.processors.JSONRenderer(),
        ],
        logger_factory=structlog.PrintLoggerFactory(file=stream),
        wrapper_class=structlog.make_filtering_bound_logger(20),
        cache_logger_on_first_use=False,
    )

    structlog.get_logger("test").info("account_state", worker_account_id=7, **REDACTED_FIELDS)
    rendered = stream.getvalue()

    assert rendered.strip(), "nothing was logged"
    payload = json.loads(rendered.strip().splitlines()[-1])

    for key, secret in REDACTED_FIELDS.items():
        assert payload[key] == "***REDACTED***", f"{key} was not redacted"
        assert secret not in rendered, f"secret value for {key} leaked into the log line"

    # Non-secret context still comes through.
    assert payload["worker_account_id"] == 7
    structlog.reset_defaults()


def test_redact_processor_is_installed_by_configure_logging():
    """The redactor must be wired into the real configuration, not just testable."""
    import structlog

    from stories_monitor.logging_setup import _redact, configure_logging

    configure_logging(json_output=True)
    processors = structlog.get_config()["processors"]
    assert _redact in processors, "configure_logging() did not install the redactor"
    structlog.reset_defaults()
