"""
Database connector service.
Builds SQLAlchemy engines for source and target databases dynamically.
Provides methods to:
  - Test a connection
  - Introspect table schema (column names + types)
  - Count rows (with optional WHERE)
  - Read rows in batches (chunked for large tables)
  - Write rows (insert / upsert) with conflict handling
"""
from __future__ import annotations
import logging
from contextlib import contextmanager
from typing import Any, Generator

from sqlalchemy import create_engine, text, inspect, Table, MetaData
from sqlalchemy.engine import Engine
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.exc import SQLAlchemyError, IntegrityError

from app.schemas.migration import ConflictStrategy

log = logging.getLogger(__name__)


class DBConnector:
    """
    Manages a single database connection (source or target).
    Uses SQLAlchemy Core (not ORM) for maximum flexibility across DB types.
    """

    def __init__(self, url: str):
        self.url    = url
        self.engine = self._create_engine(url)
        self.db_type = self._detect_type(url)

    def _create_engine(self, url: str) -> Engine:
        kwargs: dict = {
            "pool_pre_ping": True,
            "pool_size"    : 5,
            "max_overflow" : 10,
        }
        # SQLite doesn't support connection pooling args
        if url.startswith("sqlite"):
            kwargs = {}
        return create_engine(url, **kwargs)

    def _detect_type(self, url: str) -> str:
        if "postgresql" in url:
            return "postgresql"
        if "mysql" in url:
            return "mysql"
        return "sqlite"

    @contextmanager
    def connect(self):
        """Context manager for a single connection."""
        with self.engine.connect() as conn:
            yield conn

    def test_connection(self) -> dict[str, Any]:
        """Verify the connection is alive. Returns info dict."""
        try:
            with self.connect() as conn:
                result = conn.execute(text("SELECT 1")).scalar()
                return {"ok": True, "db_type": self.db_type, "ping": result}
        except SQLAlchemyError as e:
            return {"ok": False, "error": str(e)}

    def list_tables(self) -> list[str]:
        """Return all table names in the connected database."""
        inspector = inspect(self.engine)
        return inspector.get_table_names()

    def get_column_info(self, table_name: str) -> list[dict[str, str]]:
        """
        Returns column metadata: [{name, type, nullable, primary_key}]
        Used by dry-run to show schema before migration.
        """
        inspector = inspect(self.engine)
        columns   = inspector.get_columns(table_name)
        # constrained_columns is a plain list of column-name strings
        pk_cols   = set(inspector.get_pk_constraint(table_name).get("constrained_columns", []))
        return [
            {
                "name"       : col["name"],
                "type"       : str(col["type"]),
                "nullable"   : col.get("nullable", True),
                "primary_key": col["name"] in pk_cols,
            }
            for col in columns
        ]

    def get_primary_keys(self, table_name: str) -> list[str]:
        """Return primary key column names for a table."""
        inspector = inspect(self.engine)
        pk = inspector.get_pk_constraint(table_name)
        return pk.get("constrained_columns", [])

    def count_rows(self, table_name: str, where: str | None = None) -> int:
        """Count rows in a table, optionally filtered."""
        sql = f"SELECT COUNT(*) FROM {table_name}"
        if where:
            sql += f" WHERE {where}"
        with self.connect() as conn:
            return conn.execute(text(sql)).scalar() or 0

    def read_batches(
        self,
        table_name : str,
        batch_size : int = 1000,
        where      : str | None = None,
        order_by   : str | None = None,
        columns    : list[str] | None = None,
    ) -> Generator[list[dict[str, Any]], None, None]:
        """
        Generator that yields rows in batches using OFFSET/LIMIT pagination.
        Each batch is a list of dicts: [{col: val, ...}, ...]

        Why OFFSET pagination?
          Simple and works across all DB types.
          Limitation: slow on very large offsets (millions of rows).
          Production upgrade: use keyset pagination on PK for >10M row tables.
        """
        col_expr = ", ".join(columns) if columns else "*"
        base_sql = f"SELECT {col_expr} FROM {table_name}"
        if where:
            base_sql += f" WHERE {where}"
        if order_by:
            base_sql += f" ORDER BY {order_by}"

        offset = 0
        with self.connect() as conn:
            while True:
                batch_sql = f"{base_sql} LIMIT {batch_size} OFFSET {offset}"
                rows = conn.execute(text(batch_sql)).mappings().fetchall()
                if not rows:
                    break
                yield [dict(r) for r in rows]
                if len(rows) < batch_size:
                    break
                offset += batch_size

    def write_batch(
        self,
        table_name        : str,
        rows              : list[dict[str, Any]],
        conflict_strategy : ConflictStrategy = ConflictStrategy.skip,
        primary_keys      : list[str] | None = None,
        dry_run           : bool = False,
    ) -> dict[str, int]:
        """
        Write a batch of rows to the target table.

        Conflict handling:
          skip      → INSERT ... ON CONFLICT DO NOTHING
          overwrite → INSERT ... ON CONFLICT DO UPDATE SET (upsert)
          error     → plain INSERT, raises on duplicate

        Returns: {"written": n, "skipped": n, "errors": n[, "error_detail": str]}
        """
        if dry_run or not rows:
            return {"written": len(rows), "skipped": 0, "errors": 0}

        stats: dict = {"written": 0, "skipped": 0, "errors": 0}

        try:
            with self.engine.begin() as conn:
                if self.db_type == "postgresql":
                    stats = self._pg_write(conn, table_name, rows, conflict_strategy, primary_keys)
                elif self.db_type == "mysql":
                    stats = self._mysql_write(conn, table_name, rows, conflict_strategy)
                else:
                    stats = self._generic_write(conn, table_name, rows, conflict_strategy)
        except SQLAlchemyError as e:
            log.error("Batch write failed on table %s: %s", table_name, e)
            stats["errors"] = len(rows)
            # Surface the reason so the caller can fail loudly instead of
            # reporting a "completed" migration that wrote nothing.
            stats["error_detail"] = str(getattr(e, "orig", None) or e)[:500]
        return stats

    def _pg_write(self, conn, table_name, rows, strategy, pk_cols):
        """PostgreSQL-specific insert using ON CONFLICT clause."""
        meta  = MetaData()
        table = Table(table_name, meta, autoload_with=self.engine)
        stmt  = pg_insert(table).values(rows)

        if strategy == ConflictStrategy.skip:
            stmt = stmt.on_conflict_do_nothing()
        elif strategy == ConflictStrategy.overwrite and pk_cols:
            update_cols = {c: stmt.excluded[c] for c in rows[0] if c not in pk_cols}
            stmt = stmt.on_conflict_do_update(index_elements=pk_cols, set_=update_cols)

        result = conn.execute(stmt)
        written = result.rowcount if result.rowcount >= 0 else len(rows)
        skipped = len(rows) - written
        return {"written": max(0, written), "skipped": max(0, skipped), "errors": 0}

    def _mysql_write(self, conn, table_name, rows, strategy):
        """MySQL-specific insert using INSERT IGNORE or INSERT ... ON DUPLICATE KEY UPDATE."""
        meta  = MetaData()
        table = Table(table_name, meta, autoload_with=self.engine)

        if strategy == ConflictStrategy.skip:
            # INSERT IGNORE silently skips duplicates
            stmt = mysql_insert(table).prefix_with("IGNORE").values(rows)
            result = conn.execute(stmt)
        elif strategy == ConflictStrategy.overwrite:
            stmt = mysql_insert(table).values(rows)
            update_dict = {c: stmt.inserted[c] for c in rows[0]}
            stmt = stmt.on_duplicate_key_update(**update_dict)
            result = conn.execute(stmt)
        else:
            # strategy == error → plain insert, will raise on duplicate
            stmt = table.insert().values(rows)
            result = conn.execute(stmt)

        written = result.rowcount if result.rowcount >= 0 else len(rows)
        return {"written": max(0, written), "skipped": max(0, len(rows) - written), "errors": 0}

    def _generic_write(self, conn, table_name, rows, strategy):
        """SQLite / fallback — plain INSERT with try/except per batch."""
        meta  = MetaData()
        table = Table(table_name, meta, autoload_with=self.engine)
        written = skipped = errors = 0
        for row in rows:
            try:
                conn.execute(table.insert().values(**row))
                written += 1
            except IntegrityError:
                # Only a genuine duplicate/constraint hit counts as a "skip".
                # Anything else (missing column, bad type...) must propagate so
                # the caller records a real error instead of a silent skip.
                if strategy == ConflictStrategy.error:
                    raise
                skipped += 1
        return {"written": written, "skipped": skipped, "errors": errors}

    def dispose(self):
        """Release connection pool. Call when migration is done."""
        self.engine.dispose()
