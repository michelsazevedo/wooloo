"""Application configuration.

Single source of truth for runtime configuration. Values are read from the process
environment first, falling back to the ``.env`` file at the repository root.

"""

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

_REPO_ROOT = Path(__file__).resolve().parents[3]

_ENV_FILE = _REPO_ROOT / ".env"


class Settings(BaseSettings):
    """Typed application settings.

    Attributes:
        database_url: Async SQLAlchemy DSN for PostgreSQL, e.g.
            ``postgresql+asyncpg://user:password@host:5432/dbname``.
    """

    model_config = SettingsConfigDict(
        env_file=_ENV_FILE,
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: str


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide ``Settings`` instance.

    Cached so the ``.env`` file is parsed once per process. Suitable for use as a
    FastAPI dependency via ``Depends(get_settings)``.

    Returns:
        The shared, immutable-by-convention settings instance.

    Raises:
        pydantic.ValidationError: If a required setting is absent from both the
            environment and the ``.env`` file.
    """
    return Settings()
