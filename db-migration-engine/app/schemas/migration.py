"""Pydantic v2 schemas — request validation and response serialization."""
from pydantic import BaseModel, Field, field_validator, model_validator
from datetime import datetime
from typing import Any
from enum import Enum


# ── Enums (mirrors models) ────────────────────────────────────────────────────

class DBType(str, Enum):
    postgresql = "postgresql"
    mysql      = "mysql"
    sqlite     = "sqlite"


class ConflictStrategy(str, Enum):
    skip      = "skip"
    overwrite = "overwrite"
    error     = "error"


class TransformType(str, Enum):
    none           = "none"           # pass through unchanged
    rename         = "rename"         # rename column (target key handles this)
    mask_email     = "mask_email"     # user@domain.com → u***@domain.com
    mask_phone     = "mask_phone"     # 9876543210 → ******3210
    mask_full      = "mask_full"      # replace with [REDACTED]
    cast_int       = "cast_int"       # cast to integer
    cast_str       = "cast_str"       # cast to string
    cast_bool      = "cast_bool"      # cast to boolean
    uppercase      = "uppercase"      # string to uppercase
    lowercase      = "lowercase"      # string to lowercase
    strip          = "strip"          # strip whitespace
    tinyint_to_bool= "tinyint_to_bool"# MySQL TINYINT(1) → Python bool


# ── Column mapping schema ─────────────────────────────────────────────────────

class ColumnMapping(BaseModel):
    source    : str = Field(..., description="Source column name")
    target    : str = Field(..., description="Target column name (can differ)")
    transform : TransformType = TransformType.none
    skip      : bool = Field(False, description="Exclude this column from migration")


class TableMapping(BaseModel):
    source_table     : str = Field(..., description="Table name in source DB")
    target_table     : str = Field(..., description="Table name in target DB")
    columns          : list[ColumnMapping] = Field(default_factory=list,
                           description="Per-column rules. Empty = migrate all columns as-is")
    conflict_strategy: ConflictStrategy | None = None  # overrides global if set
    where_clause     : str | None = Field(None, description="Optional SQL WHERE filter on source")
    order_by         : str | None = Field(None, description="Optional ORDER BY for deterministic migration")


# ── Connection profile ────────────────────────────────────────────────────────

class ConnectionProfileCreate(BaseModel):
    name       : str = Field(..., min_length=3, max_length=120)
    db_type    : DBType
    url        : str = Field(..., description="Full DB URL e.g. mysql+pymysql://user:pass@host:3306/db")
    description: str | None = None

    @field_validator("url")
    @classmethod
    def validate_url(cls, v: str, info) -> str:
        db_type = info.data.get("db_type")
        if db_type == DBType.postgresql and not v.startswith("postgresql"):
            raise ValueError("URL must start with 'postgresql://' for PostgreSQL connections")
        if db_type == DBType.mysql and "mysql" not in v:
            raise ValueError("URL must contain 'mysql' for MySQL connections")
        return v


class ConnectionProfileOut(BaseModel):
    model_config = {"from_attributes": True}
    id         : str
    name       : str
    db_type    : str
    description: str | None
    created_at : datetime
    is_active  : bool


# ── Migration config ──────────────────────────────────────────────────────────

class MigrationConfigCreate(BaseModel):
    name             : str = Field(..., min_length=3, max_length=200)
    description      : str | None = None
    source_conn_id   : str
    target_conn_id   : str
    table_mappings   : list[TableMapping] = Field(..., min_length=1)
    conflict_strategy: ConflictStrategy = ConflictStrategy.skip
    batch_size       : int = Field(1000, ge=100, le=10000)

    @model_validator(mode="after")
    def source_and_target_differ(self) -> "MigrationConfigCreate":
        if self.source_conn_id == self.target_conn_id:
            raise ValueError("source_conn_id and target_conn_id must be different connections")
        return self


class MigrationConfigOut(BaseModel):
    model_config = {"from_attributes": True}
    id               : str
    name             : str
    description      : str | None
    source_conn_id   : str
    target_conn_id   : str
    table_mappings   : list[dict]
    conflict_strategy: str
    batch_size       : int
    created_at       : datetime
    is_active        : bool


# ── Run schemas ───────────────────────────────────────────────────────────────

class RunTrigger(BaseModel):
    is_dry_run : bool = Field(False, description="Dry run = read + transform only, no writes")


class TableStatOut(BaseModel):
    model_config = {"from_attributes": True}
    source_table : str
    target_table : str
    status       : str
    rows_read    : int
    rows_written : int
    rows_skipped : int
    rows_errored : int
    started_at   : datetime | None
    completed_at : datetime | None
    error_detail : str | None


class MigrationRunOut(BaseModel):
    model_config = {"from_attributes": True}
    id             : str
    config_id      : str
    celery_task_id : str | None
    status         : str
    is_dry_run     : bool
    total_tables   : int
    total_rows_read: int
    total_written  : int
    total_skipped  : int
    total_errors   : int
    duration_sec   : float | None
    error_message  : str | None
    started_at     : datetime
    completed_at   : datetime | None
    table_stats    : list[TableStatOut] = []


# ── Dry-run preview ───────────────────────────────────────────────────────────

class DryRunTablePreview(BaseModel):
    source_table    : str
    target_table    : str
    source_row_count: int
    sample_original : list[dict[str, Any]]  # first 3 rows raw
    sample_transformed: list[dict[str, Any]] # first 3 rows after transforms
    columns_mapped  : int
    columns_skipped : int
    transforms_applied: list[str]
    warnings        : list[str]


class DryRunResult(BaseModel):
    config_id         : str
    config_name       : str
    tables            : list[DryRunTablePreview]
    total_rows_to_migrate: int
    estimated_duration_sec: float
    warnings          : list[str]
