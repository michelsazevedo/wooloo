"""
Unit tests for the store's duplicate-name narrowing.

`_is_duplicate_name_violation` decides whether a failed flush was a collision on
`repositories.name`, and therefore whether the caller is answered with a `409` or
an honest `500`. The integration suite reaches it only through the one error the
live database can actually be provoked into raising, which exercises the `True`
branch and nothing else: a version that ignored the constraint name, and a version
that *required* one, both pass every test in
`tests/integration/test_repository_store.py`.

The driver exceptions are therefore stood in for rather than provoked. The two
attributes the function reads — `sqlstate` on SQLAlchemy's asyncpg DBAPI shim, and
`constraint_name` on the asyncpg exception chained onto it — are the whole of its
input, and the cases that matter most (another constraint, an absent constraint
name, another SQLSTATE) are ones this one-table schema cannot produce on demand.
The wrapper itself is a real `IntegrityError`, so the function is still called with
the type it declares.

"""

from typing import Final

import pytest
from sqlalchemy.exc import IntegrityError

from wooloo.infrastructure.repositories.store import _is_duplicate_name_violation

UNIQUE_VIOLATION: Final = "23505"

NAME_INDEX: Final = "ix_repositories_name"
"""
The SQLSTATE and the index name above are written literally rather than imported
from the store, because neither is the store's to choose: `23505` is PostgreSQL's,
and `ix_repositories_name` is what migration `0002` creates. Importing the
constants would let a rename in the store agree with itself while silently ceasing
to match the database.

"""

_STATEMENT: Final = "INSERT INTO repositories (name) VALUES ($1)"

_UNREAD_ORIG: Final = Exception("read only to build the wrapper's own message")


class DriverError(Exception):
    """Stand-in for the DBAPI shim, which reports a SQLSTATE and no constraint.

    Attributes:
        sqlstate: The PostgreSQL error code the shim exposes.
    """

    def __init__(self, sqlstate: str) -> None:
        """Initialize the stand-in.

        Args:
            sqlstate: The error code to report.
        """
        super().__init__("stand-in driver error")
        self.sqlstate = sqlstate


class ConstraintViolation(Exception):
    """Stand-in for the chained asyncpg exception, which names the constraint.

    Attributes:
        constraint_name: The violated constraint, or `None` for a driver that
            reports the violation without naming it.
    """

    def __init__(self, constraint_name: str | None) -> None:
        """Initialize the stand-in.

        Args:
            constraint_name: The constraint to name, or `None`.
        """
        super().__init__("stand-in constraint violation")
        self.constraint_name = constraint_name


def wrap(driver_error: BaseException | None) -> IntegrityError:
    """Wrap a driver error the way SQLAlchemy wraps one onto a failed flush.

    `orig` is assigned after construction rather than passed in, because
    `IntegrityError.__init__` reads it to build its own message and so refuses
    `None`, while the attribute it sets is documented as optional — which is the
    state the function under test is expected to survive.

    Args:
        driver_error: The exception to expose as `error.orig`.

    Returns:
        A real `IntegrityError` carrying it.
    """
    error = IntegrityError(_STATEMENT, None, _UNREAD_ORIG)
    error.orig = driver_error

    return error


def flush_failure(sqlstate: str, cause: BaseException | None = None) -> IntegrityError:
    """Build the error a failed flush raises, carrying only what the function reads.

    Args:
        sqlstate: The SQLSTATE the DBAPI shim reports.
        cause: The exception chained onto the shim, or `None` for a driver that
            chains nothing.

    Returns:
        The wrapped stand-in.
    """
    driver_error = DriverError(sqlstate)
    driver_error.__cause__ = cause

    return wrap(driver_error)


def test_a_unique_violation_on_the_name_index_is_a_duplicate_name() -> None:
    """
    The case the live database produces: both signals present and both matching.

    """
    error = flush_failure(UNIQUE_VIOLATION, ConstraintViolation(NAME_INDEX))

    assert _is_duplicate_name_violation(error) is True


def test_a_unique_violation_on_another_constraint_is_not_a_duplicate_name() -> None:
    """A unique violation elsewhere in the table must not become a `409`.

    This is what the constraint check buys, and it is unreachable from the
    integration suite: `repositories` carries exactly one unique index today, so
    the second one this table acquires would be the first caller told its name was
    taken when it was not.
    """
    error = flush_failure(UNIQUE_VIOLATION, ConstraintViolation("ix_repositories_digest"))

    assert _is_duplicate_name_violation(error) is False


@pytest.mark.parametrize(
    "cause",
    [
        pytest.param(None, id="nothing-chained"),
        pytest.param(ConstraintViolation(None), id="chained-without-a-name"),
        pytest.param(Exception("a driver that reports no constraint"), id="no-such-attribute"),
    ],
)
def test_a_unique_violation_degrades_to_the_sqlstate_alone(cause: BaseException | None) -> None:
    """An unavailable constraint name must weaken the check, not defeat it.

    A future driver version, or a different DBAPI, may chain nothing or chain
    something that does not name the constraint. Demanding the name would turn
    every such duplicate into a `500`; the SQLSTATE alone is still a real
    narrowing over treating any integrity failure as a taken name.

    Args:
        cause: What the shim chains, in each of the three ways the name can be
            missing.
    """
    error = flush_failure(UNIQUE_VIOLATION, cause)

    assert _is_duplicate_name_violation(error) is True


@pytest.mark.parametrize(
    "sqlstate",
    [
        pytest.param("23502", id="not-null-violation"),
        pytest.param("23503", id="foreign-key-violation"),
        pytest.param("23514", id="check-violation"),
        pytest.param("54000", id="program-limit-exceeded"),
    ],
)
def test_another_sqlstate_is_never_a_duplicate_name(sqlstate: str) -> None:
    """Only `23505` may be translated, whatever constraint the driver names.

    Each error is given the *matching* constraint name on purpose, so the SQLSTATE
    gate is the only thing that can produce the `False`. `23514` is the live case:
    `ck_repositories_name_length` fires on an oversized name, and answering that
    with a `409` would tell the caller a name is taken when the input was simply
    too long.

    Args:
        sqlstate: A PostgreSQL error code that is not a unique violation.
    """
    error = flush_failure(sqlstate, ConstraintViolation(NAME_INDEX))

    assert _is_duplicate_name_violation(error) is False


def test_an_error_carrying_no_driver_exception_is_not_a_duplicate_name() -> None:
    """
    With no `orig` there is no SQLSTATE to narrow on, so the honest answer is that
    this is not a known duplicate.

    """
    assert _is_duplicate_name_violation(wrap(None)) is False


def test_a_driver_error_reporting_no_sqlstate_is_not_a_duplicate_name() -> None:
    """
    A DBAPI that exposes no `sqlstate` at all must be read as "unknown" rather than
    crash the handler with an `AttributeError`.

    """
    assert _is_duplicate_name_violation(wrap(Exception("a DBAPI with no sqlstate"))) is False
