"""
End-to-end tests: real source DB -> transforms -> real target DB.
Uses two SQLite files so no external services are needed.
Exercises the actual connector + migration service (no mocking).
"""
import os
import uuid
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.db.session import Base, engine as internal_engine
from app.models.migration import (
    ConnectionProfile, MigrationConfig, MigrationRun, RunStatus,
)
from app.services.migration_service import MigrationService

SRC_PATH = "./e2e_source.db"
TGT_PATH = "./e2e_target.db"


@pytest.fixture
def dbs():
    for p in (SRC_PATH, TGT_PATH):
        if os.path.exists(p):
            os.remove(p)

    src = create_engine(f"sqlite:///{SRC_PATH}")
    tgt = create_engine(f"sqlite:///{TGT_PATH}")

    with src.begin() as c:
        c.execute(text(
            "CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT, "
            "email TEXT, phone TEXT, is_active INTEGER)"
        ))
        for i in range(1, 26):   # 25 rows -> forces multiple batches at batch_size=10
            c.execute(text(
                "INSERT INTO users VALUES (:i, :u, :e, :p, :a)"
            ), {"i": i, "u": f"User{i}", "e": f"user{i}@example.com",
                "p": f"98765432{i:02d}", "a": i % 2})

    with tgt.begin() as c:
        c.execute(text(
            "CREATE TABLE members (id INTEGER PRIMARY KEY, username TEXT, "
            "email TEXT, phone TEXT, is_active INTEGER)"
        ))
    yield src, tgt
    src.dispose(); tgt.dispose()
    for p in (SRC_PATH, TGT_PATH):
        if os.path.exists(p):
            os.remove(p)


@pytest.fixture
def db_session():
    Base.metadata.create_all(bind=internal_engine)
    S = sessionmaker(bind=internal_engine)
    s = S()
    yield s
    s.close()


def _make_config(db, strategy="skip", batch_size=100):
    tag = uuid.uuid4().hex[:8]
    src = ConnectionProfile(name=f"src-{tag}", db_type="sqlite", url_enc=f"sqlite:///{SRC_PATH}")
    tgt = ConnectionProfile(name=f"tgt-{tag}", db_type="sqlite", url_enc=f"sqlite:///{TGT_PATH}")
    db.add_all([src, tgt]); db.commit()
    cfg = MigrationConfig(
        name=f"cfg-{tag}",
        source_conn_id=src.id, target_conn_id=tgt.id,
        conflict_strategy=strategy, batch_size=batch_size,
        table_mappings=[{
            "source_table": "users", "target_table": "members",
            "columns": [
                {"source": "username", "target": "username", "transform": "lowercase"},
                {"source": "email",    "target": "email",    "transform": "mask_email"},
                {"source": "phone",    "target": "phone",    "transform": "mask_phone"},
                {"source": "is_active","target": "is_active","transform": "tinyint_to_bool"},
            ],
        }],
    )
    db.add(cfg); db.commit()
    return cfg


def test_dry_run_reads_and_transforms_but_writes_nothing(dbs, db_session):
    src, tgt = dbs
    cfg = _make_config(db_session)
    result = MigrationService(db_session).dry_run(cfg)

    assert result.total_rows_to_migrate == 25
    preview = result.tables[0]
    assert preview.source_row_count == 25
    assert len(preview.sample_original) == 3
    # transforms visibly applied to the sample
    assert preview.sample_transformed[0]["username"] == "user1"
    assert "*" in preview.sample_transformed[0]["email"]
    # target must be untouched
    with tgt.connect() as c:
        assert c.execute(text("SELECT COUNT(*) FROM members")).scalar() == 0


def test_live_migration_multiple_batches(dbs, db_session):
    src, tgt = dbs
    cfg = _make_config(db_session, batch_size=10)   # 25 rows -> 3 batches
    run = MigrationRun(config_id=cfg.id, status=RunStatus.pending)
    db_session.add(run); db_session.commit()

    run = MigrationService(db_session).run_migration(run)

    assert run.status == RunStatus.completed, run.error_message
    assert run.total_rows_read == 25
    assert run.total_written == 25
    with tgt.connect() as c:
        assert c.execute(text("SELECT COUNT(*) FROM members")).scalar() == 25
        row = c.execute(text("SELECT username, email, phone FROM members WHERE id=1")).one()
    assert row.username == "user1"                 # lowercased
    assert row.email.startswith("u") and "*" in row.email   # masked
    assert row.phone.startswith("*")               # masked


def test_rerun_is_idempotent_with_skip_strategy(dbs, db_session):
    """Running the same migration twice must not duplicate or crash."""
    src, tgt = dbs
    cfg = _make_config(db_session, strategy="skip", batch_size=10)
    for _ in range(2):
        run = MigrationRun(config_id=cfg.id, status=RunStatus.pending)
        db_session.add(run); db_session.commit()
        run = MigrationService(db_session).run_migration(run)
        assert run.status == RunStatus.completed, run.error_message
    with tgt.connect() as c:
        assert c.execute(text("SELECT COUNT(*) FROM members")).scalar() == 25


# ── Regression tests: failures must be loud, never silent ────────────────────

def test_missing_target_table_fails_preflight_with_clear_message(dbs, db_session):
    """Regression: used to crash mid-run with a cryptic 'NoSuchTableError: products'."""
    src, tgt = dbs
    cfg = _make_config(db_session)
    cfg.table_mappings = cfg.table_mappings + [{
        "source_table": "users", "target_table": "table_that_does_not_exist", "columns": [],
    }]
    db_session.commit()
    run = MigrationRun(config_id=cfg.id, status=RunStatus.pending)
    db_session.add(run); db_session.commit()

    run = MigrationService(db_session).run_migration(run)

    assert run.status == RunStatus.failed
    assert "table_that_does_not_exist" in run.error_message
    assert "create the schema first" in run.error_message
    # preflight runs first, so NOTHING was written to the valid table either
    with tgt.connect() as c:
        assert c.execute(text("SELECT COUNT(*) FROM members")).scalar() == 0
    assert run.duration_sec is not None


def test_write_error_marks_run_failed_not_completed(dbs, db_session):
    """Regression: a target with a missing column used to report 'completed, 0 written'."""
    src, tgt = dbs
    with tgt.begin() as c:   # target is missing the email/phone columns
        c.execute(text("DROP TABLE members"))
        c.execute(text("CREATE TABLE members (id INTEGER PRIMARY KEY, username TEXT)"))
    cfg = _make_config(db_session)
    run = MigrationRun(config_id=cfg.id, status=RunStatus.pending)
    db_session.add(run); db_session.commit()

    run = MigrationService(db_session).run_migration(run)

    assert run.status == RunStatus.failed          # NOT completed
    assert run.total_written == 0
    assert "members" in run.error_message
    assert run.table_stats[0].status == RunStatus.failed
