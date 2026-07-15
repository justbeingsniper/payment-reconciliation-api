"""Application configuration, driven entirely by environment variables.

Local dev needs zero setup: if DATABASE_URL is unset we fall back to a local
SQLite file. In production (Render) we inject a Postgres DATABASE_URL.
"""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # SQLite by default => `uvicorn app.main:app` works with no database install.
    database_url: str = "sqlite:///./setu.db"

    # App metadata
    app_name: str = "Setu Reconciliation Service"
    app_version: str = "1.0.0"

    # A transaction stuck in INITIATED longer than this is flagged as a
    # "stuck / pending" reconciliation discrepancy.
    stuck_threshold_hours: int = 24

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    @property
    def normalized_database_url(self) -> str:
        """Render/Heroku hand out `postgres://` URLs; SQLAlchemy 2.x wants
        the explicit `postgresql+psycopg2://` driver form."""
        url = self.database_url
        if url.startswith("postgres://"):
            url = url.replace("postgres://", "postgresql+psycopg2://", 1)
        return url


@lru_cache
def get_settings() -> Settings:
    return Settings()
