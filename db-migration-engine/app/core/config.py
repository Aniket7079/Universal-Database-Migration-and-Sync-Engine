from pydantic_settings import BaseSettings, SettingsConfigDict
from functools import lru_cache


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # App
    APP_ENV: str = "development"
    SECRET_KEY: str = "dev-secret-key-change-in-production"
    APP_HOST: str = "0.0.0.0"
    APP_PORT: int = 8000
    DEBUG: bool = True

    # Internal DB (stores migration metadata)
    DATABASE_URL: str = "postgresql://migrator:migrator123@localhost:5432/migrationdb"

    # Redis / Celery
    REDIS_URL: str = "redis://localhost:6379/0"
    CELERY_BROKER_URL: str = "redis://localhost:6379/0"
    CELERY_RESULT_BACKEND: str = "redis://localhost:6379/1"

    # Batch processing
    DEFAULT_BATCH_SIZE: int = 1000
    MAX_BATCH_SIZE: int = 10000
    MAX_CONCURRENT_MIGRATIONS: int = 5

    # Supported DB types
    SUPPORTED_DB_TYPES: list[str] = ["postgresql", "mysql", "sqlite"]


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
