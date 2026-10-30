"""
Celery application and task definitions.

Worker start command:
  celery -A app.tasks.celery_app worker --loglevel=info -Q migrations

Monitor via Flower:
  celery -A app.tasks.celery_app flower --port=5555
"""
from celery import Celery
from app.core.config import settings

celery_app = Celery(
    "db_migration_engine",
    broker=settings.CELERY_BROKER_URL,
    backend=settings.CELERY_RESULT_BACKEND,
    include=["app.tasks.migration_tasks"],
)

celery_app.conf.update(
    task_serializer    = "json",
    accept_content     = ["json"],
    result_serializer  = "json",
    timezone           = "UTC",
    enable_utc         = True,
    task_track_started = True,
    task_acks_late     = True,            # Re-queue if worker crashes mid-task
    worker_prefetch_multiplier = 1,       # One task per worker at a time
    task_routes        = {
        "app.tasks.migration_tasks.run_migration_task": {"queue": "migrations"},
    },
    task_annotations   = {
        "app.tasks.migration_tasks.run_migration_task": {
            "rate_limit": f"{settings.MAX_CONCURRENT_MIGRATIONS}/m"
        }
    },
)
