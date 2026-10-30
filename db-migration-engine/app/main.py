"""
DB Migration Engine — FastAPI application entry point.
"""
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.core.config import settings
from app.db.session import engine, Base
from app.api.routes.migrations import router as migration_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Create all DB tables on startup."""
    Base.metadata.create_all(bind=engine)
    print("✅ Database tables created / verified")
    yield
    print("👋 Shutting down DB Migration Engine")


app = FastAPI(
    title       = "DB Migration Engine",
    description = "Universal database migration and sync engine — MySQL ↔ PostgreSQL ↔ SQLite",
    version     = "1.0.0",
    lifespan    = lifespan,
    docs_url    = "/docs",
    redoc_url   = "/redoc",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins  = ["*"],
    allow_methods  = ["*"],
    allow_headers  = ["*"],
)

app.include_router(migration_router)


@app.get("/", tags=["health"])
def root():
    return {
        "service" : "DB Migration Engine",
        "version" : "1.0.0",
        "status"  : "running",
        "docs"    : "/docs",
    }


@app.get("/health", tags=["health"])
def health():
    from sqlalchemy import text
    from app.db.session import SessionLocal
    db_ok = False
    try:
        db = SessionLocal()
        db.execute(text("SELECT 1"))
        db.close()
        db_ok = True
    except Exception:
        pass
    return JSONResponse(
        status_code=200 if db_ok else 503,
        content={"status": "ok" if db_ok else "degraded", "database": db_ok}
    )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "app.main:app",
        host   = settings.APP_HOST,
        port   = settings.APP_PORT,
        reload = settings.DEBUG,
    )
