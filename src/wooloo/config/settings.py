"""Application configuration.

Single source of truth for runtime configuration. Values are read from the process
environment first, falling back to the ``.env`` file at the repository root.

"""

from functools import lru_cache
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from wooloo.config.storage import _VALID_STORAGE_BACKENDS

_REPO_ROOT = Path(__file__).resolve().parents[3]

_ENV_FILE = _REPO_ROOT / ".env"

_VALID_LOG_LEVELS = frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})

_VALID_OTLP_SCHEMES = ("http://", "https://")

_DEFAULT_MAX_UPLOAD_BYTES = 5 * 1024 * 1024 * 1024
"""
Default request body ceiling, 5 GiB, expressed as a product rather than as
``5368709120`` so that the unit it is counted in stays visible.

"""


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
        storage_backend: Which blob storage backend to build, one of
            ``filesystem``, ``s3``, ``minio``, ``gcs``. Accepted in any case and
            normalised to lower case; defaults to ``filesystem``. Only
            ``filesystem`` is implemented — the other three are recognised names
            that fail in the storage factory, not here (see
            :mod:`wooloo.config.storage`).
        storage_root: Directory the filesystem backend stores blobs under.
            Defaults to ``/var/lib/wooloo``. The previous default, ``/tmp/wooloo``,
            was wrong on two counts: ``/tmp`` is cleared on reboot on most systems,
            and it is world-writable, so any local unprivileged user can create the
            tree — or plant a symlink at a digest path, which is predictable from
            public content — before this process ever writes to it. Its sticky bit
            does not help: that only stops one user *deleting* another's entries,
            not creating new ones. Unused by the other backends. Not validated at
            startup — the directory need not exist yet, and whether it is usable is
            reported by the storage health check rather than by failing the boot.
        max_upload_bytes: Largest request body accepted on any endpoint, enforced
            by :class:`~wooloo.api.middleware.max_body_size.MaxBodySizeMiddleware`.
            Defaults to 5 GiB, a plausible ceiling for a single OCI layer. Must be
            positive. There is no "unlimited" setting: stored blobs are never
            reclaimed — garbage collection is out of scope — so an oversized upload
            that succeeds costs that disk permanently rather than transiently.
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

    storage_backend: str = "filesystem"

    storage_root: str = "/var/lib/wooloo"

    max_upload_bytes: int = _DEFAULT_MAX_UPLOAD_BYTES

    @field_validator("max_upload_bytes")
    @classmethod
    def _require_positive_upload_limit(cls, value: int) -> int:
        """Reject a limit that would refuse every upload.

        Args:
            value: The raw limit from the environment or ``.env``.

        Returns:
            The limit unchanged.

        Raises:
            ValueError: If the limit is zero or negative. pydantic wraps this in a
                ``ValidationError``, so the typo fails the boot rather than turning
                every upload into a ``413``.
        """
        if value <= 0:
            raise ValueError("must be a positive number of bytes")

        return value

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

    @field_validator("storage_backend")
    @classmethod
    def _normalise_storage_backend(cls, value: str) -> str:
        """Lower-case the configured backend and reject unknown names.

        Args:
            value: The raw backend name from the environment or ``.env``.

        Returns:
            The backend name in lower case.

        Raises:
            ValueError: If the name is not one of the recognised backends.
                pydantic wraps this in a ``ValidationError``, so a typo fails the
                boot rather than surfacing on the first blob request.
        """
        normalised = value.strip().lower()

        if normalised not in _VALID_STORAGE_BACKENDS:
            expected = ", ".join(sorted(_VALID_STORAGE_BACKENDS))
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
