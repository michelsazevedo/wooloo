"""Application configuration.

Single source of truth for runtime configuration. Values are read from the process
environment first, falling back to the ``.env`` file at the repository root.

"""

from functools import lru_cache
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_REPO_ROOT = Path(__file__).resolve().parents[3]

_ENV_FILE = _REPO_ROOT / ".env"

_VALID_LOG_LEVELS = frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})

_VALID_OTLP_SCHEMES = ("http://", "https://")


class Settings(BaseSettings):
    """Typed application settings.

    Attributes:
        database_url: Async SQLAlchemy DSN for PostgreSQL, e.g.
            ``postgresql+asyncpg://user:password@host:5432/dbname``.
        log_level: Threshold applied to the root logger, one of ``DEBUG``,
            ``INFO``, ``WARNING``, ``ERROR``, ``CRITICAL``. Accepted in any case
            and normalised to upper case; defaults to ``INFO``.
        otel_exporter_otlp_endpoint: OTLP gRPC endpoint spans are exported to,
            e.g. ``http://localhost:4317``. Defaults to the Jaeger service
            published by the local ``docker-compose.yml``. Only the scheme is
            validated — see :meth:`_require_otlp_scheme` for why validation stops
            there. An endpoint that is well-formed but unreachable degrades to
            dropped spans rather than failing startup, since tracing may not take
            the app down. One narrow exception to that: a malformed IPv6-shaped
            value (``http://[::1:4317``, an unclosed bracket) raises ``ValueError``
            inside ``OTLPSpanExporter``'s constructor, which propagates out of
            :func:`~wooloo.infrastructure.telemetry.tracing.configure_tracing` and
            does fail the boot. That is a typo in a hand-edited endpoint rather
            than a collector outage, so failing loudly on it is acceptable; the
            scheme check below deliberately does not try to catch it.
        otel_console_export_enabled: Whether spans are additionally printed to
            stdout by a console exporter. Defaults to ``True`` so a local boot is
            observable with no collector running. Worth turning off wherever
            stdout is ingested by a log pipeline: a single span was measured at
            roughly 9.4x the byte volume of a normal log line, per request, and
            that amplification buys nothing once traces are browsable in Jaeger.
    """

    model_config = SettingsConfigDict(
        env_file=_ENV_FILE,
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: str

    log_level: str = "INFO"

    otel_exporter_otlp_endpoint: str = "http://localhost:4317"

    otel_console_export_enabled: bool = True

    @field_validator("otel_exporter_otlp_endpoint")
    @classmethod
    def _require_otlp_scheme(cls, value: str) -> str:
        """Reject an endpoint whose scheme does not state whether to use TLS.

        Args:
            value: The raw endpoint from the environment or ``.env``.

        Returns:
            The endpoint unchanged.

        Raises:
            ValueError: If the value starts with neither ``http://`` nor
                ``https://``. pydantic wraps this in a ``ValidationError``.
        """
        if not value.startswith(_VALID_OTLP_SCHEMES):
            expected = " or ".join(_VALID_OTLP_SCHEMES)
            raise ValueError(
                f"must start with {expected} — the scheme decides whether the exporter "
                f"uses TLS, and omitting it silently drops every span"
            )

        return value

    @field_validator("log_level")
    @classmethod
    def _normalise_log_level(cls, value: str) -> str:
        """Upper-case the configured level and reject unknown names.

        Args:
            value: The raw level name from the environment or ``.env``.

        Returns:
            The level name in upper case.

        Raises:
            ValueError: If the name is not a standard Python logging level.
                pydantic wraps this in a ``ValidationError``.
        """
        normalised = value.strip().upper()

        if normalised not in _VALID_LOG_LEVELS:
            expected = ", ".join(sorted(_VALID_LOG_LEVELS))
            raise ValueError(f"must be one of {expected} (case-insensitive)")

        return normalised


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide ``Settings`` instance.

    Returns:
        The shared, immutable-by-convention settings instance.

    Raises:
        pydantic.ValidationError: If a required setting is absent from both the
            environment and the ``.env`` file.
    """
    return Settings()
