"""Celery tasks for async migration execution."""
import logging
from celery import Task
from app.tasks.celery_app import celery_app
from app.db.session import SessionLocal
from app.models.migration import MigrationRun, RunStatus
from app.services.migration_service import MigrationService

log = logging.getLogger(__name__)


class MigrationTask(Task):
    """Custom base task that handles DB session lifecycle."""
    abstract = True

    def on_failure(self, exc, task_id, args, kwargs, einfo):
        """Update run status to failed if task dies unexpectedly."""
        run_id = args[0] if args else kwargs.get("run_id")
        if run_id:
            db = SessionLocal()
            try:
                run = db.get(MigrationRun, run_id)
                if run and run.status == RunStatus.running:
                    run.status        = RunStatus.failed
                    run.error_message = f"Worker crashed: {str(exc)[:500]}"
                    db.commit()
            finally:
                db.close()


@celery_app.task(
    bind=True,
    base=MigrationTask,
    name="app.tasks.migration_tasks.run_migration_task",
    max_retries=0,          # migrations should not auto-retry — data safety
    time_limit=14400,       # 4 hour hard limit per migration
    soft_time_limit=13800,  # 3h50m soft limit (triggers SoftTimeLimitExceeded)
)
def run_migration_task(self, run_id: str) -> dict:
    """
    Celery task: execute a MigrationRun.

    Called by:  POST /api/v1/runs/{config_id}/trigger
    Args:       run_id (str) — MigrationRun.id from the database

    Returns summary dict for the Celery result backend.
    """
    log.info("Celery task started: run_id=%s task_id=%s", run_id, self.request.id)

    db = SessionLocal()
    try:
        run = db.get(MigrationRun, run_id)
        if not run:
            raise ValueError(f"MigrationRun {run_id} not found")

        # Store Celery task ID so we can poll it from FastAPI
        run.celery_task_id = self.request.id
        db.commit()

        # Run the migration
        svc     = MigrationService(db)
        run     = svc.run_migration(run)

        summary = {
            "run_id"        : run.id,
            "status"        : run.status.value,
            "total_read"    : run.total_rows_read,
            "total_written" : run.total_written,
            "total_skipped" : run.total_skipped,
            "total_errors"  : run.total_errors,
            "duration_sec"  : run.duration_sec,
        }
        log.info("Migration run %s finished: %s", run_id, summary)
        return summary

    except Exception as exc:
        log.exception("Migration run %s failed", run_id)
        raise exc
    finally:
        db.close()
