"""
Migration service — orchestrates the full migration flow.

Called by both:
  1. Celery task (async background run)
  2. FastAPI dry-run endpoint (synchronous preview)

Flow:
  For each table in config.table_mappings:
    a. Read source rows in batches
    b. Apply column transforms
    c. Coerce types (MySQL → PostgreSQL)
    d. Write to target with conflict handling
    e. Record stats per batch
"""
from __future__ import annotations
import logging
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from app.models.migration import MigrationConfig, MigrationRun, TableRunStat, AuditLog, RunStatus
from app.schemas.migration import (
    TableMapping, ConflictStrategy, DryRunResult, DryRunTablePreview
)
from app.services.connector import DBConnector
from app.services.transformer import apply_table_transforms, coerce_mysql_row

log = logging.getLogger(__name__)


class MigrationService:

    def __init__(self, db: Session):
        self.db = db

    # ── Dry run ───────────────────────────────────────────────────────────────

    def dry_run(self, config: MigrationConfig) -> DryRunResult:
        """
        Read source, apply transforms, return preview — NO writes to target.
        Shows first 3 rows before and after transforms for each table.
        """
        source = DBConnector(self._decrypt_url(config.source_conn_id))
        previews: list[DryRunTablePreview] = []
        total_rows = 0
        global_warnings: list[str] = []

        for mapping_dict in config.table_mappings:
            mapping = TableMapping(**mapping_dict)
            warnings: list[str] = []

            # Check table exists in source
            source_tables = source.list_tables()
            if mapping.source_table not in source_tables:
                warnings.append(f"Table '{mapping.source_table}' not found in source DB")
                previews.append(DryRunTablePreview(
                    source_table=mapping.source_table,
                    target_table=mapping.target_table,
                    source_row_count=0,
                    sample_original=[],
                    sample_transformed=[],
                    columns_mapped=0,
                    columns_skipped=0,
                    transforms_applied=[],
                    warnings=warnings,
                ))
                continue

            row_count = source.count_rows(mapping.source_table, mapping.where_clause)
            total_rows += row_count

            # Read first 3 rows as sample
            sample_raw: list[dict] = []
            for batch in source.read_batches(mapping.source_table, batch_size=3,
                                              where=mapping.where_clause,
                                              order_by=mapping.order_by):
                sample_raw = batch[:3]
                break

            # Apply transforms to sample
            sample_transformed, applied = apply_table_transforms(sample_raw, mapping)

            # Detect warnings
            schema = source.get_column_info(mapping.source_table)
            schema_cols = {c["name"] for c in schema}
            mapped_cols = {m.source for m in mapping.columns}
            missing_cols = mapped_cols - schema_cols
            for mc in missing_cols:
                warnings.append(f"Mapped column '{mc}' does not exist in source table")

            previews.append(DryRunTablePreview(
                source_table=mapping.source_table,
                target_table=mapping.target_table,
                source_row_count=row_count,
                sample_original=[self._serialize_row(r) for r in sample_raw],
                sample_transformed=[self._serialize_row(r) for r in sample_transformed],
                columns_mapped=len([m for m in mapping.columns if not m.skip]),
                columns_skipped=len([m for m in mapping.columns if m.skip]),
                transforms_applied=applied,
                warnings=warnings,
            ))

        source.dispose()
        estimated_sec = total_rows / 5000  # rough estimate: 5000 rows/sec

        return DryRunResult(
            config_id=config.id,
            config_name=config.name,
            tables=previews,
            total_rows_to_migrate=total_rows,
            estimated_duration_sec=round(estimated_sec, 1),
            warnings=global_warnings,
        )

    # ── Live migration ────────────────────────────────────────────────────────

    def run_migration(self, run: MigrationRun) -> MigrationRun:
        """
        Execute the full migration for a MigrationRun.
        Called by the Celery task — runs in a background worker.
        """
        config = run.config
        source = DBConnector(self._decrypt_url(config.source_conn_id))
        target = DBConnector(self._decrypt_url(config.target_conn_id))

        run.status     = RunStatus.running
        run.started_at = datetime.utcnow()
        self.db.commit()

        try:
            # Fail fast BEFORE touching any data: every table must exist.
            self._preflight(config, source, target)

            for mapping_dict in config.table_mappings:
                mapping = TableMapping(**mapping_dict)
                self._migrate_table(run, config, source, target, mapping)

            run.status       = RunStatus.dry_run if run.is_dry_run else RunStatus.completed
            run.completed_at = datetime.utcnow()
            run.duration_sec = (run.completed_at - run.started_at).total_seconds()

        except Exception as e:
            log.exception("Migration run %s failed", run.id)
            run.status        = RunStatus.failed
            run.error_message = str(e)[:2000]
            run.completed_at  = datetime.utcnow()
            run.duration_sec  = (run.completed_at - run.started_at).total_seconds()

        finally:
            # Aggregate totals from table stats
            run.total_tables    = len(run.table_stats)
            run.total_rows_read = sum(s.rows_read    for s in run.table_stats)
            run.total_written   = sum(s.rows_written for s in run.table_stats)
            run.total_skipped   = sum(s.rows_skipped for s in run.table_stats)
            run.total_errors    = sum(s.rows_errored for s in run.table_stats)
            self.db.commit()
            source.dispose()
            target.dispose()

        return run

    def _preflight(self, config: MigrationConfig, source: DBConnector, target: DBConnector):
        """
        Verify every source and target table exists before migrating anything.
        Gives one clear error listing ALL problems, and guarantees we never
        end up half-migrated because the 2nd table was missing.
        """
        source_tables = set(source.list_tables())
        target_tables = set(target.list_tables())
        problems: list[str] = []
        for m in config.table_mappings:
            if m["source_table"] not in source_tables:
                problems.append(f"source table '{m['source_table']}' does not exist")
            if m["target_table"] not in target_tables:
                problems.append(
                    f"target table '{m['target_table']}' does not exist "
                    f"(this engine copies data, it does not create tables — create the schema first)"
                )
        if problems:
            raise ValueError("Pre-flight check failed: " + "; ".join(problems))

    def _migrate_table(
        self,
        run    : MigrationRun,
        config : MigrationConfig,
        source : DBConnector,
        target : DBConnector,
        mapping: TableMapping,
    ):
        """Migrate a single table: read → transform → write in batches."""
        table_stat = TableRunStat(
            run_id=run.id,
            source_table=mapping.source_table,
            target_table=mapping.target_table,
            status=RunStatus.running,
            started_at=datetime.utcnow(),
        )
        self.db.add(table_stat)
        self.db.commit()

        pk_cols  = target.get_primary_keys(mapping.target_table)
        strategy = mapping.conflict_strategy or ConflictStrategy(config.conflict_strategy)
        is_mysql_source = source.db_type == "mysql"

        try:
            col_info = source.get_column_info(mapping.source_table)
            col_types = {c["name"]: c["type"] for c in col_info}

            for batch in source.read_batches(
                mapping.source_table,
                batch_size=config.batch_size,
                where=mapping.where_clause,
                order_by=mapping.order_by,
            ):
                table_stat.rows_read += len(batch)

                # Step 1: Coerce MySQL types if needed
                if is_mysql_source and target.db_type == "postgresql":
                    batch = [coerce_mysql_row(row, col_types) for row in batch]

                # Step 2: Apply column transforms
                transformed, _ = apply_table_transforms(batch, mapping)

                # Step 3: Write to target (or skip if dry run)
                stats = target.write_batch(
                    table_name=mapping.target_table,
                    rows=transformed,
                    conflict_strategy=strategy,
                    primary_keys=pk_cols,
                    dry_run=run.is_dry_run,
                )
                table_stat.rows_written += stats["written"]
                table_stat.rows_skipped += stats["skipped"]
                table_stat.rows_errored += stats["errors"]

                if stats["errors"]:
                    # Never report success when rows could not be written.
                    raise RuntimeError(
                        f"Write to target table '{mapping.target_table}' failed: "
                        f"{stats.get('error_detail', 'unknown error')}"
                    )

                # Commit progress every batch so we can track live
                self.db.commit()

                log.info(
                    "Table %s: batch done — read=%d written=%d skipped=%d",
                    mapping.source_table,
                    table_stat.rows_read,
                    table_stat.rows_written,
                    table_stat.rows_skipped,
                )

            table_stat.status       = RunStatus.completed
            table_stat.completed_at = datetime.utcnow()

        except Exception as e:
            table_stat.status       = RunStatus.failed
            table_stat.error_detail = str(e)[:2000]
            table_stat.completed_at = datetime.utcnow()
            self.db.commit()
            raise

        self.db.commit()

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _decrypt_url(self, conn_id: str) -> str:
        """
        Retrieve and decrypt the connection URL from connection_profiles.
        In Week 1 we return the URL as-is (no encryption).
        In Week 3: integrate with app/services/encryption.py using Fernet.
        """
        from app.models.migration import ConnectionProfile
        profile = self.db.get(ConnectionProfile, conn_id)
        if not profile:
            raise ValueError(f"Connection profile {conn_id} not found")
        return profile.url_enc  # Week 3: decrypt with Fernet here

    def _serialize_row(self, row: dict) -> dict:
        """Make row JSON-serializable (handles datetime, Decimal, etc.)."""
        import decimal
        result = {}
        for k, v in row.items():
            if isinstance(v, decimal.Decimal):
                result[k] = float(v)
            elif hasattr(v, "isoformat"):
                result[k] = v.isoformat()
            else:
                result[k] = v
        return result
