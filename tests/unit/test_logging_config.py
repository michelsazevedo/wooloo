"""
Unit tests for `configure_logging()` — the real pipeline, reading real output.

"""

import io
import json
import logging
import re
from datetime import UTC, datetime
from importlib.metadata import version
from typing import Any, Final

import pytest
import structlog

from wooloo.config.settings import get_settings
from wooloo.infrastructure.logging import config as logging_config
from wooloo.infrastructure.logging.config import configure_logging
from wooloo.infrastructure.logging.logger import logger

pytestmark = pytest.mark.usefixtures("pristine_logging_state")

STUB_DATABASE_URL: Final = "postgresql+asyncpg://user:password@localhost:5432/wooloo"
"""
A parseable DSN, present only because `Settings` requires one.

Nothing here builds an engine or opens a socket — `configure_logging()` reads
`log_level` and nothing else — but `Settings` refuses to construct without a
`database_url`, and leaving the real one to be picked up from `.env` would make
these tests depend on a file they have no interest in.

"""

FOREIGN_LOGGER: Final = "thirdparty"
"""
Stands in for any library that logs through stdlib `logging` — uvicorn, SQLAlchemy.

"""

SERVICE_NAME: Final = "wooloo"
"""
Expected value of the ambient ``service`` field stamped on every record.

"""

DISTRIBUTION_NAME: Final = "wooloo"
"""
Installed distribution the ambient ``version`` field must be read from.

Spelled separately from `SERVICE_NAME` because `config.py` keeps them separate for
a reason that outlives their coinciding today: one names the service a record is
attributed to, the other names the package a version number is looked up in, and
only the second is a name a packaging change could move.

"""

ISO_8601_UTC: Final = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|\+00:00)"
)
"""
Shape of a UTC ISO-8601 timestamp: date, ``T``, time, and an explicit UTC marker.

The trailing marker is the part worth pinning. A timestamper configured without
``utc=True`` still emits a well-formed ISO-8601 string — of local time, unmarked —
and every downstream consumer would read it as UTC and be silently wrong by the
deployment's offset.

"""


def configure_and_capture(monkeypatch: pytest.MonkeyPatch, log_level: str = "INFO") -> io.StringIO:
    """Run the real `configure_logging()` and point its handler at a buffer.

    Args:
        monkeypatch: Used to set the environment `Settings` reads, and undo it
            afterwards.
        log_level: The `LOG_LEVEL` the application is configured with.

    Returns:
        The buffer receiving everything the configured handler emits from here
        on. The redirect happens *after* configuration, so anything
        `configure_logging()` might emit while running cannot land in it.

    Raises:
        AssertionError: If `configure_logging()` did not leave exactly one
            stream handler on the root logger, which would mean the rest of this
            file is asserting on the wrong sink.
    """
    monkeypatch.setenv("DATABASE_URL", STUB_DATABASE_URL)
    monkeypatch.setenv("LOG_LEVEL", log_level)
    get_settings.cache_clear()

    configure_logging()

    handlers = logging.getLogger().handlers
    assert len(handlers) == 1
    handler = handlers[0]
    assert isinstance(handler, logging.StreamHandler)

    stream = io.StringIO()
    handler.setStream(stream)

    return stream


def emitted_records(stream: io.StringIO) -> list[dict[str, Any]]:
    """Parse everything written to the buffer as one JSON object per line.

    Args:
        stream: The buffer returned by :func:`configure_and_capture`.

    Returns:
        One parsed record per non-blank line, in emission order.

    Raises:
        AssertionError: If any line is not valid JSON. This is the assertion that
            fails when the renderer is swapped for a human-readable one: the
            console renderer's output is perfectly readable and completely
            unparseable, which is exactly the regression that would otherwise
            reach production and break ingestion for every line in the service.
    """
    records: list[dict[str, Any]] = []

    for line in stream.getvalue().splitlines():
        if not line.strip():
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError as error:
            raise AssertionError(f"log line is not valid JSON: {line!r}") from error

    return records


def contains_key(value: Any, key: str) -> bool:
    """Report whether a key appears anywhere in a nested structure.

    Args:
        value: Any JSON-shaped value.
        key: The key to search for at every depth.

    Returns:
        `True` if any mapping at any depth carries `key`.
    """
    if isinstance(value, dict):
        return key in value or any(contains_key(item, key) for item in value.values())

    if isinstance(value, list):
        return any(contains_key(item, key) for item in value)

    return False


@pytest.fixture(autouse=True)
def clear_settings_cache() -> None:
    """Drop the cached `Settings` so the next test reads its own environment.

    `get_settings` is `lru_cache`d and process-wide. A `Settings` built here from
    a monkeypatched `LOG_LEVEL` would otherwise outlive the test that set it and
    be handed to whatever asks next, including code in other modules.
    """
    get_settings.cache_clear()


def test_a_structured_event_renders_as_json_carrying_the_documented_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every application log line must be a JSON object with the contract's fields.

    `event`, `level`, `logger` and `timestamp` are what the PRD promises every
    record carries, and they are what a query in a log aggregator filters on: a
    record missing `level` cannot be alerted on, and one missing `logger` cannot
    be attributed. The extra `foo` keyword pins the reason for structured logging
    in the first place — variable data arrives as its own queryable field rather
    than interpolated into prose.
    """
    stream = configure_and_capture(monkeypatch)

    logger.info("plain_event", foo="bar")

    records = emitted_records(stream)
    assert len(records) == 1

    record = records[0]
    assert record["event"] == "plain_event"
    assert record["level"] == "info"
    assert record["logger"] == __name__
    assert record["foo"] == "bar"

    timestamp = record["timestamp"]
    assert ISO_8601_UTC.fullmatch(timestamp)
    assert datetime.fromisoformat(timestamp).tzinfo == UTC


def test_bound_context_reaches_the_rendered_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Context bound for the request must survive all the way into the output.

    """
    stream = configure_and_capture(monkeypatch)

    structlog.contextvars.bind_contextvars(request_id="req-42")
    logger.info("correlated_event")

    records = emitted_records(stream)
    assert len(records) == 1
    assert records[0]["event"] == "correlated_event"
    assert records[0]["request_id"] == "req-42"


def test_every_record_carries_the_service_name_and_the_resolved_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Every line out of this pipeline must say which service, at which version.

    """
    stream = configure_and_capture(monkeypatch)

    logger.info("application_started")
    logging.getLogger(FOREIGN_LOGGER).warning("uvicorn_startup_chatter")

    records = emitted_records(stream)
    assert [record["event"] for record in records] == [
        "application_started",
        "uvicorn_startup_chatter",
    ]
    assert [record["service"] for record in records] == [SERVICE_NAME, SERVICE_NAME]
    assert [record["version"] for record in records] == [
        version(DISTRIBUTION_NAME),
        version(DISTRIBUTION_NAME),
    ]


def test_the_ambient_version_is_read_from_installed_package_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    The reported version must be looked up, not restated in the source.

    """
    looked_up: list[str] = []
    sentinel_version = "1999.12.31-metadata-sentinel"

    def fake_version(distribution: str) -> str:
        looked_up.append(distribution)
        return sentinel_version

    monkeypatch.setattr(logging_config, "version", fake_version)

    stream = configure_and_capture(monkeypatch)

    logger.info("plain_event")
    logger.info("another_event")

    records = emitted_records(stream)
    assert len(records) == 2
    assert looked_up == [DISTRIBUTION_NAME]
    assert [record["version"] for record in records] == [sentinel_version, sentinel_version]


def test_an_exception_renders_a_structured_traceback_and_never_local_variables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A logged exception must ship its traceback as data, and only its traceback.

    """
    stream = configure_and_capture(monkeypatch)

    try:
        raise ValueError("kaboom")
    except ValueError:
        logger.exception("exploded")

    records = emitted_records(stream)
    assert len(records) == 1

    record = records[0]
    assert record["event"] == "exploded"
    assert record["level"] == "error"

    rendered = record["exception"]
    assert isinstance(rendered, list)
    assert len(rendered) >= 1
    assert rendered[0]["exc_type"] == "ValueError"
    assert rendered[0]["exc_value"] == "kaboom"

    frames = rendered[0]["frames"]
    assert frames
    assert all(set(frame) == {"filename", "lineno", "name"} for frame in frames)
    assert not contains_key(record, "locals")


def test_a_foreign_stdlib_record_renders_as_json_with_its_extra_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Third-party log lines must be JSON too, keeping the data they attached.

    """
    stream = configure_and_capture(monkeypatch)

    logging.getLogger(FOREIGN_LOGGER).warning("foreign_record", extra={"foo": "bar"})

    records = emitted_records(stream)
    assert len(records) == 1

    record = records[0]
    assert record["event"] == "foreign_record"
    assert record["level"] == "warning"
    assert record["logger"] == FOREIGN_LOGGER
    assert record["foo"] == "bar"
    assert ISO_8601_UTC.fullmatch(record["timestamp"])


def test_the_configured_log_level_suppresses_anything_below_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`LOG_LEVEL` must actually change what is emitted, in both directions.

    """
    stream = configure_and_capture(monkeypatch, log_level="ERROR")

    logger.info("below_the_threshold")
    assert emitted_records(stream) == []

    logger.error("at_the_threshold")

    records = emitted_records(stream)
    assert len(records) == 1
    assert records[0]["event"] == "at_the_threshold"
    assert records[0]["level"] == "error"


def test_the_configured_log_level_also_binds_a_logger_that_sets_its_own_level(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A raised `LOG_LEVEL` must quieten libraries that pin their own level.

    """
    stream = configure_and_capture(monkeypatch, log_level="ERROR")

    uvicorn_logger = logging.getLogger("uvicorn.error")
    uvicorn_logger.setLevel(logging.INFO)

    uvicorn_logger.info("uvicorn_startup_chatter")
    assert emitted_records(stream) == []

    uvicorn_logger.error("uvicorn_startup_failed")

    records = emitted_records(stream)
    assert len(records) == 1
    assert records[0]["event"] == "uvicorn_startup_failed"
    assert records[0]["logger"] == "uvicorn.error"


def test_debug_opens_up_wooloo_without_turning_on_third_party_debug(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    `LOG_LEVEL=DEBUG` must reach wooloo's own code and stop there.

    """
    stream = configure_and_capture(monkeypatch, log_level="DEBUG")

    logging.getLogger("wooloo.example").debug("wooloo_internal_detail")
    logging.getLogger(FOREIGN_LOGGER).debug("library_internal_detail")

    records = emitted_records(stream)
    assert [record["event"] for record in records] == ["wooloo_internal_detail"]
    assert records[0]["logger"] == "wooloo.example"
