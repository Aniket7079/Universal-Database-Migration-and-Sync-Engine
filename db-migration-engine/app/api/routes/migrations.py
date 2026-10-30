"""
FastAPI router — all migration engine endpoints.

Endpoints:
  POST   /connections            — register a DB connection profile
  GET    /connections            — list all connection profiles
  DELETE /connections/{id}       — delete a connection profile
  POST   /connections/{id}/test  — test a connection (live ping)

  POST   /configs                — create a migration config
  GET    /configs                — list all migration configs
  GET    /configs/{id}           — get a specific config
  DELETE /configs/{id}           — delete a config

  POST   /configs/{id}/dry-run   — preview migration without writing
  POST   /configs/{id}/run       — trigger live migration (Celery task)
  GET    /runs/{run_id}          — get run status + table stats
  GET    /runs/{run_id}/progress — lightweight progress polling
  POST   /runs/{run_id}/cancel   — cancel a running migration

  GET    /runs                   — list all runs (paginated)
"""
from fastapi import APIRouter, Depends, HTTPException, Query, BackgroundTasks
from sqlalchemy.orm import Session
from datetime import datetime

from app.db.session import get_db
from app.models.migration import (
    ConnectionProfile, MigrationConfig, MigrationRun, RunStatus
)
from app.schemas.migration import (
    ConnectionProfileCreate, ConnectionProfileOut,
    MigrationConfigCreate, MigrationConfigOut,
    RunTrigger, MigrationRunOut, DryRunResult,
)
from app.services.connector import DBConnector
from app.services.migration_service import MigrationService

router = APIRouter(prefix="/api/v1", tags=["migrations"])


# ── Connection profiles ───────────────────────────────────────────────────────

@router.post("/connections", response_model=ConnectionProfileOut, status_code=201)
def create_connection(payload: ConnectionProfileCreate, db: Session = Depends(get_db)):
    """
    Register a source or target database connection.
    The URL is stored as-is in Week 1.
    Week 3: encrypt with Fernet before storing in url_enc.
    """
    existing = db.query(ConnectionProfile).filter_by(name=payload.name).first()
    if existing:
        raise HTTPException(409, f"Connection named '{payload.name}' already exists")

    profile = ConnectionProfile(
        name       = payload.name,
        db_type    = payload.db_type.value,
        url_enc    = payload.url,   # TODO Week 3: encrypt here
        description= payload.description,
    )
    db.add(profile)
    db.commit()
    db.refresh(profile)
    return profile


@router.get("/connections", response_model=list[ConnectionProfileOut])
def list_connections(db: Session = Depends(get_db)):
    return db.query(ConnectionProfile).filter_by(is_active=True).all()


@router.delete("/connections/{conn_id}", status_code=204)
def delete_connection(conn_id: str, db: Session = Depends(get_db)):
    profile = db.get(ConnectionProfile, conn_id)
    if not profile:
        raise HTTPException(404, "Connection not found")
    profile.is_active = False
    db.commit()


@router.post("/connections/{conn_id}/test")
def test_connection(conn_id: str, db: Session = Depends(get_db)):
    """Live-ping the database to verify credentials and reachability."""
    profile = db.get(ConnectionProfile, conn_id)
    if not profile:
        raise HTTPException(404, "Connection not found")
    connector = DBConnector(profile.url_enc)
    result    = connector.test_connection()
    connector.dispose()
    if not result["ok"]:
        raise HTTPException(422, f"Connection failed: {result.get('error')}")
    return result


# ── Migration configs ─────────────────────────────────────────────────────────

@router.post("/configs", response_model=MigrationConfigOut, status_code=201)
def create_config(payload: MigrationConfigCreate, db: Session = Depends(get_db)):
    """Create a migration config with table mappings and transform rules."""
    # Validate both connections exist
    for conn_id, label in [(payload.source_conn_id, "source"), (payload.target_conn_id, "target")]:
        if not db.get(ConnectionProfile, conn_id):
            raise HTTPException(404, f"{label} connection '{conn_id}' not found")

    existing = db.query(MigrationConfig).filter_by(name=payload.name).first()
    if existing:
        raise HTTPException(409, f"Config named '{payload.name}' already exists")

    config = MigrationConfig(
        name             = payload.name,
        description      = payload.description,
        source_conn_id   = payload.source_conn_id,
        target_conn_id   = payload.target_conn_id,
        table_mappings   = [m.model_dump() for m in payload.table_mappings],
        conflict_strategy= payload.conflict_strategy.value,
        batch_size       = payload.batch_size,
    )
    db.add(config)
    db.commit()
    db.refresh(config)
    return config


@router.get("/configs", response_model=list[MigrationConfigOut])
def list_configs(db: Session = Depends(get_db)):
    return db.query(MigrationConfig).filter_by(is_active=True).all()


@router.get("/configs/{config_id}", response_model=MigrationConfigOut)
def get_config(config_id: str, db: Session = Depends(get_db)):
    config = db.get(MigrationConfig, config_id)
    if not config:
        raise HTTPException(404, "Migration config not found")
    return config


@router.delete("/configs/{config_id}", status_code=204)
def delete_config(config_id: str, db: Session = Depends(get_db)):
    config = db.get(MigrationConfig, config_id)
    if not config:
        raise HTTPException(404, "Migration config not found")
    config.is_active = False
    db.commit()


# ── Dry run ───────────────────────────────────────────────────────────────────

@router.post("/configs/{config_id}/dry-run", response_model=DryRunResult)
def dry_run(config_id: str, db: Session = Depends(get_db)):
    """
    Preview the migration — reads source, applies all transforms,
    returns sample rows before + after. Zero writes to target DB.

    Use this ALWAYS before running a live migration.
    """
    config = db.get(MigrationConfig, config_id)
    if not config:
        raise HTTPException(404, "Migration config not found")
    svc = MigrationService(db)
    return svc.dry_run(config)


# ── Trigger live migration ────────────────────────────────────────────────────

@router.post("/configs/{config_id}/run", response_model=MigrationRunOut, status_code=202)
def trigger_run(
    config_id: str,
    payload  : RunTrigger,
    db       : Session = Depends(get_db),
):
    """
    Trigger a migration run. Returns immediately with run_id.
    The migration executes in a background Celery worker.
    Poll GET /runs/{run_id} for live status.
    """
    config = db.get(MigrationConfig, config_id)
    if not config:
        raise HTTPException(404, "Migration config not found")

    # Check for already-running migration on same config
    active = db.query(MigrationRun).filter(
        MigrationRun.config_id == config_id,
        MigrationRun.status    == RunStatus.running,
    ).first()
    if active:
        raise HTTPException(409, f"Migration already running: run_id={active.id}")

    # Create run record
    run = MigrationRun(
        config_id  = config_id,
        status     = RunStatus.pending,
        is_dry_run = payload.is_dry_run,
    )
    db.add(run)
    db.commit()
    db.refresh(run)

    # Dispatch to Celery
    from app.tasks.migration_tasks import run_migration_task
    task = run_migration_task.apply_async(args=[run.id], queue="migrations")

    run.celery_task_id = task.id
    db.commit()
    db.refresh(run)
    return run


# ── Run status ────────────────────────────────────────────────────────────────

@router.get("/runs/{run_id}", response_model=MigrationRunOut)
def get_run(run_id: str, db: Session = Depends(get_db)):
    """Full run details including per-table stats."""
    run = db.get(MigrationRun, run_id)
    if not run:
        raise HTTPException(404, "Run not found")
    return run


@router.get("/runs/{run_id}/progress")
def get_progress(run_id: str, db: Session = Depends(get_db)):
    """
    Lightweight progress endpoint — poll every 2 seconds from a frontend.
    Returns totals only (no per-table details) for minimal payload.
    """
    run = db.query(MigrationRun).filter_by(id=run_id).first()
    if not run:
        raise HTTPException(404, "Run not found")
    return {
        "run_id"       : run.id,
        "status"       : run.status.value,
        "rows_read"    : run.total_rows_read,
        "rows_written" : run.total_written,
        "rows_skipped" : run.total_skipped,
        "rows_errors"  : run.total_errors,
        "duration_sec" : run.duration_sec,
        "is_dry_run"   : run.is_dry_run,
    }


@router.post("/runs/{run_id}/cancel", status_code=200)
def cancel_run(run_id: str, db: Session = Depends(get_db)):
    """
    Request cancellation of a running migration.
    Sends Celery REVOKE + marks status as cancelled.
    Note: batches already written are NOT undone. Because the default
    'skip' strategy is idempotent, simply re-running the config is safe.
    """
    run = db.get(MigrationRun, run_id)
    if not run:
        raise HTTPException(404, "Run not found")
    if run.status not in (RunStatus.running, RunStatus.pending):
        raise HTTPException(409, f"Cannot cancel run with status '{run.status}'")

    if run.celery_task_id:
        from app.tasks.celery_app import celery_app
        celery_app.control.revoke(run.celery_task_id, terminate=True, signal="SIGTERM")

    run.status       = RunStatus.cancelled
    run.completed_at = datetime.utcnow()
    db.commit()
    return {"message": f"Run {run_id} cancelled", "celery_task_id": run.celery_task_id}


@router.get("/runs", response_model=list[MigrationRunOut])
def list_runs(
    config_id: str | None = Query(None),
    status   : str | None = Query(None),
    limit    : int        = Query(20, le=100),
    offset   : int        = Query(0),
    db       : Session    = Depends(get_db),
):
    """List migration runs with optional filtering."""
    q = db.query(MigrationRun)
    if config_id:
        q = q.filter(MigrationRun.config_id == config_id)
    if status:
        q = q.filter(MigrationRun.status == status)
    return q.order_by(MigrationRun.started_at.desc()).offset(offset).limit(limit).all()
