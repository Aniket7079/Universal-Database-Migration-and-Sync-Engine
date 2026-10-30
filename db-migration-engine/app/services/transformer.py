"""
Transform engine — applies per-column rules to each row during migration.

This is the heart of the migration engine. Every row from the source DB
passes through apply_row_transforms() before being written to the target.

Supported transforms (see TransformType enum in schemas):
  none            — pass through unchanged
  rename          — handled by column mapping (source → target key)
  mask_email      — user@domain.com → u***@domain.com
  mask_phone      — 9876543210 → ******3210
  mask_full       — [REDACTED]
  cast_int/str/bool
  uppercase / lowercase / strip
  tinyint_to_bool — MySQL TINYINT(1) → True/False
"""
from __future__ import annotations
import re
import logging
from typing import Any
from app.schemas.migration import ColumnMapping, TableMapping, TransformType

log = logging.getLogger(__name__)


# ── Individual transform functions ────────────────────────────────────────────

def _mask_email(value: Any) -> str:
    """user@domain.com → u***@domain.com"""
    if not value:
        return value
    s = str(value)
    if "@" not in s:
        return s
    local, domain = s.split("@", 1)
    if len(local) <= 1:
        return f"{local}***@{domain}"
    return f"{local[0]}{'*' * (len(local) - 1)}@{domain}"


def _mask_phone(value: Any) -> str:
    """9876543210 → ******3210  (keeps last 4 digits)"""
    if not value:
        return value
    digits = re.sub(r"\D", "", str(value))
    if len(digits) < 4:
        return "****"
    return "*" * (len(digits) - 4) + digits[-4:]


def _cast_bool(value: Any) -> bool | None:
    """Handles MySQL TINYINT(1) and various bool representations."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return bool(value)
    if isinstance(value, str):
        return value.lower() in ("1", "true", "yes", "t", "y")
    return bool(value)


TRANSFORM_FUNCTIONS: dict[str, callable] = {
    TransformType.none           : lambda v: v,
    TransformType.mask_email     : _mask_email,
    TransformType.mask_phone     : _mask_phone,
    TransformType.mask_full      : lambda v: "[REDACTED]" if v is not None else None,
    TransformType.cast_int       : lambda v: int(v) if v is not None else None,
    TransformType.cast_str       : lambda v: str(v) if v is not None else None,
    TransformType.cast_bool      : _cast_bool,
    TransformType.tinyint_to_bool: _cast_bool,
    TransformType.uppercase      : lambda v: str(v).upper() if v is not None else None,
    TransformType.lowercase      : lambda v: str(v).lower() if v is not None else None,
    TransformType.strip          : lambda v: str(v).strip() if v is not None else None,
    TransformType.rename         : lambda v: v,  # rename is handled by key mapping below
}


# ── Row-level transformation ──────────────────────────────────────────────────

def apply_row_transforms(
    row: dict[str, Any],
    column_mappings: list[ColumnMapping],
) -> dict[str, Any]:
    """
    Apply column-level transforms to a single source row.

    Algorithm:
      1. If column_mappings is empty → migrate all columns as-is
      2. For each column mapping:
         a. Look up source value in row
         b. Skip column if mapping.skip = True
         c. Apply transform function
         d. Write to target key (mapping.target)
      3. Columns not mentioned in mappings are migrated as-is by default

    Returns: dict ready to be inserted into the target table
    """
    if not column_mappings:
        # No mappings defined → pass all columns through unchanged
        return dict(row)

    # Build a lookup for O(1) access
    mapping_by_source = {m.source: m for m in column_mappings}
    mentioned_sources = {m.source for m in column_mappings}
    result: dict[str, Any] = {}

    for col_name, value in row.items():
        if col_name in mapping_by_source:
            mapping = mapping_by_source[col_name]
            if mapping.skip:
                continue  # explicitly excluded column
            transform_fn = TRANSFORM_FUNCTIONS.get(mapping.transform, lambda v: v)
            try:
                result[mapping.target] = transform_fn(value)
            except (ValueError, TypeError) as e:
                # Log the error, use original value as fallback
                log.warning("Transform failed on column %s value=%r: %s", col_name, value, e)
                result[mapping.target] = value
        elif col_name not in mentioned_sources:
            # Column not in mapping → migrate as-is with same name
            result[col_name] = value

    return result


def apply_table_transforms(
    rows: list[dict[str, Any]],
    table_mapping: TableMapping,
) -> tuple[list[dict[str, Any]], list[str]]:
    """
    Transform all rows for a table.
    Returns (transformed_rows, list_of_applied_transform_names).
    """
    transformed  = [apply_row_transforms(row, table_mapping.columns) for row in rows]
    applied      = list({m.transform for m in table_mapping.columns
                         if m.transform != TransformType.none})
    return transformed, [t.value for t in applied]


# ── Type coercion for MySQL → PostgreSQL ─────────────────────────────────────

MYSQL_TO_PG_TYPE_MAP = {
    "tinyint(1)" : "boolean",
    "tinyint"    : "smallint",
    "mediumint"  : "integer",
    "datetime"   : "timestamp",
    "longtext"   : "text",
    "mediumtext" : "text",
    "tinytext"   : "text",
    "double"     : "double precision",
    "float"      : "real",
}


def coerce_mysql_row(row: dict[str, Any], column_types: dict[str, str]) -> dict[str, Any]:
    """
    Fix MySQL type mismatches before inserting into PostgreSQL.
    Key problem: MySQL TINYINT(1) is used as boolean → must become True/False.
    """
    result = {}
    for col, val in row.items():
        col_type = column_types.get(col, "").lower()
        if "tinyint(1)" in col_type or col_type == "tinyint":
            result[col] = _cast_bool(val)
        else:
            result[col] = val
    return result
