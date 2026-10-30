"""
SQLAlchemy models for the migration engine internal database.

Tables:
  connection_profiles  — stored DB credentials (encrypted)
  migration_configs    — defines what to migrate (tables, transforms, rules)
  migration_runs       — each execution of a config (with status + stats)
  table_run_stats      — per-table progress within a run
  audit_log            — every row written/skipped/failed per run
"""
import uuid
from datetime import datetime
from sqlalchemy import (
    String, Text, Integer, BigInteger, Boolean, DateTime,
    ForeignKey, JSON, Enum as SAEnum, Float,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.dialects.postgresql import UUID
from app.db.session import Base
import enum


def new_uuid() -> str:
    return str(uuid.uuid4())


# ── Enums ─────────────────────────────────────────────────────────────────────

class DBType(str, enum.Enum):
    postgresql = "postgresql"
    mysql      = "mysql"
    sqlite     = "sqlite"


class RunStatus(str, enum.Enum):
    pending    = "pending"
    running    = "running"
    completed  = "completed"
    failed     = "failed"
    cancelled  = "cancelled"
    dry_run    = "dry_run"


class ConflictStrategy(str, enum.Enum):
    skip      = "skip"      # keep existing row, skip incoming
    overwrite = "overwrite" # upsert — replace existing row
    error     = "error"     # raise exception on conflict


# ── Models ────────────────────────────────────────────────────────────────────

class ConnectionProfile(Base):
    __tablename__ = "connection_profiles"

    id         : Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    name       : Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    db_type    : Mapped[str] = mapped_column(SAEnum(DBType), nullable=False)
    # Connection URL stored encrypted (see services/encryption.py)
    url_enc    : Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    created_at : Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    is_active  : Mapped[bool] = mapped_column(Boolean, default=True)

    # Relationships
    source_configs: Mapped[list["MigrationConfig"]] = relationship(
        "MigrationConfig", foreign_keys="MigrationConfig.source_conn_id", back_populates="source_conn"
    )
    target_configs: Mapped[list["MigrationConfig"]] = relationship(
        "MigrationConfig", foreign_keys="MigrationConfig.target_conn_id", back_populates="target_conn"
    )


class MigrationConfig(Base):
    """
    Defines a complete migration: source → target + table map + rules.
    One config can be run multiple times (each run = MigrationRun row).
    """
    __tablename__ = "migration_configs"

    id               : Mapped[str]  = mapped_column(String(36), primary_key=True, default=new_uuid)
    name             : Mapped[str]  = mapped_column(String(200), unique=True, nullable=False)
    description      : Mapped[str | None] = mapped_column(Text)
    source_conn_id   : Mapped[str]  = mapped_column(String(36), ForeignKey("connection_profiles.id"))
    target_conn_id   : Mapped[str]  = mapped_column(String(36), ForeignKey("connection_profiles.id"))

    # Table mappings + column transforms stored as JSON
    # Format: [{"source_table": "users", "target_table": "members",
    #            "columns": [{"source": "email", "target": "email_address",
    #                         "transform": "mask_email"}], ...}]
    table_mappings   : Mapped[dict] = mapped_column(JSON, default=list)

    # Global conflict strategy (can be overridden per table in table_mappings)
    conflict_strategy: Mapped[str]  = mapped_column(SAEnum(ConflictStrategy), default="skip")
    batch_size       : Mapped[int]  = mapped_column(Integer, default=1000)

    created_at       : Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at       : Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow,
                                                         onupdate=datetime.utcnow)
    is_active        : Mapped[bool] = mapped_column(Boolean, default=True)

    # Relationships
    source_conn: Mapped["ConnectionProfile"] = relationship(
        "ConnectionProfile", foreign_keys=[source_conn_id], back_populates="source_configs"
    )
    target_conn: Mapped["ConnectionProfile"] = relationship(
        "ConnectionProfile", foreign_keys=[target_conn_id], back_populates="target_configs"
    )
    runs: Mapped[list["MigrationRun"]] = relationship(
        "MigrationRun", back_populates="config", order_by="MigrationRun.started_at.desc()"
    )


class MigrationRun(Base):
    """Records a single execution of a MigrationConfig."""
    __tablename__ = "migration_runs"

    id             : Mapped[str]      = mapped_column(String(36), primary_key=True, default=new_uuid)
    config_id      : Mapped[str]      = mapped_column(String(36), ForeignKey("migration_configs.id"))
    celery_task_id : Mapped[str|None] = mapped_column(String(155))
    status         : Mapped[str]      = mapped_column(SAEnum(RunStatus), default="pending")
    is_dry_run     : Mapped[bool]     = mapped_column(Boolean, default=False)

    # Totals
    total_tables   : Mapped[int]   = mapped_column(Integer, default=0)
    total_rows_read: Mapped[int]   = mapped_column(BigInteger, default=0)
    total_written  : Mapped[int]   = mapped_column(BigInteger, default=0)
    total_skipped  : Mapped[int]   = mapped_column(BigInteger, default=0)
    total_errors   : Mapped[int]   = mapped_column(BigInteger, default=0)
    duration_sec   : Mapped[float|None] = mapped_column(Float)

    error_message  : Mapped[str|None] = mapped_column(Text)
    started_at     : Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    completed_at   : Mapped[datetime|None] = mapped_column(DateTime)

    # Relationships
    config     : Mapped["MigrationConfig"] = relationship("MigrationConfig", back_populates="runs")
    table_stats: Mapped[list["TableRunStat"]] = relationship(
        "TableRunStat", back_populates="run", cascade="all, delete-orphan"
    )
    audit_logs : Mapped[list["AuditLog"]] = relationship(
        "AuditLog", back_populates="run", cascade="all, delete-orphan"
    )


class TableRunStat(Base):
    """Per-table progress within a migration run."""
    __tablename__ = "table_run_stats"

    id           : Mapped[str]      = mapped_column(String(36), primary_key=True, default=new_uuid)
    run_id       : Mapped[str]      = mapped_column(String(36), ForeignKey("migration_runs.id"))
    source_table : Mapped[str]      = mapped_column(String(200))
    target_table : Mapped[str]      = mapped_column(String(200))
    status       : Mapped[str]      = mapped_column(SAEnum(RunStatus), default="pending")
    rows_read    : Mapped[int]      = mapped_column(BigInteger, default=0)
    rows_written : Mapped[int]      = mapped_column(BigInteger, default=0)
    rows_skipped : Mapped[int]      = mapped_column(BigInteger, default=0)
    rows_errored : Mapped[int]      = mapped_column(BigInteger, default=0)
    started_at   : Mapped[datetime|None] = mapped_column(DateTime)
    completed_at : Mapped[datetime|None] = mapped_column(DateTime)
    error_detail : Mapped[str|None] = mapped_column(Text)

    run: Mapped["MigrationRun"] = relationship("MigrationRun", back_populates="table_stats")


class AuditLog(Base):
    """
    Row-level audit log. Only written for errors and PII-masked rows.
    For large migrations, write summary-level only to keep this table manageable.
    """
    __tablename__ = "audit_logs"

    id           : Mapped[str]  = mapped_column(String(36), primary_key=True, default=new_uuid)
    run_id       : Mapped[str]  = mapped_column(String(36), ForeignKey("migration_runs.id"))
    table_name   : Mapped[str]  = mapped_column(String(200))
    event_type   : Mapped[str]  = mapped_column(String(50))  # written, skipped, error, pii_masked
    row_pk       : Mapped[str|None] = mapped_column(String(200))
    detail       : Mapped[str|None] = mapped_column(Text)
    occurred_at  : Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    run: Mapped["MigrationRun"] = relationship("MigrationRun", back_populates="audit_logs")
