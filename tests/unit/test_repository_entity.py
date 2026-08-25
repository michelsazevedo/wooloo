"""
Unit tests for the `Repository` domain entity.

Two properties are load-bearing here and neither is visible by reading the class
body alone.

The first is immutability: `Repository` carries data the database has already
vouched for, and every field is described as never changing or changing only
through the store. A silently mutable entity would let a caller edit a name or
backdate a timestamp in memory and hand the result on as if it were persisted.

The second is framework independence: the entity must be constructible from a
worker, a CLI, or a test with no web framework and no ORM in scope. That claim is
checked here by making those packages genuinely unimportable and importing the
module under that constraint, rather than by reading its import block — an import
pulled in indirectly, through a helper or a `TYPE_CHECKING`-adjacent shortcut,
would pass a grep and fail this test.

"""

import importlib
import sys
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import FrozenInstanceError, fields, replace
from datetime import UTC, datetime
from importlib.abc import MetaPathFinder
from importlib.machinery import ModuleSpec
from types import ModuleType
from typing import Final
from uuid import UUID, uuid4

import pytest

from wooloo.domain.repositories.entity import Repository

ENTITY_MODULE: Final = "wooloo.domain.repositories.entity"

BLOCKED_FRAMEWORKS: Final = frozenset(
    {"sqlalchemy", "fastapi", "pydantic", "starlette", "structlog"}
)
"""Every framework the domain layer is forbidden from depending on.

`pydantic` and `starlette` are listed alongside the direct dependencies because
they arrive transitively with FastAPI: an entity that imported either would be
just as unusable from a worker as one that imported FastAPI itself.
"""

FIELD_NAMES: Final = tuple(field.name for field in fields(Repository))
"""The entity's fields, read off the class rather than hard-coded.

A field added later is covered by the mutation tests automatically, which is the
point — a new mutable field is exactly the regression those tests exist to catch.
"""

SAMPLE_ID: Final = UUID("0d8f0b9e-1f4a-4c3d-8a2b-5e6f7a8b9c0d")

SAMPLE_CREATED_AT: Final = datetime(2026, 1, 15, 9, 30, tzinfo=UTC)

SAMPLE_UPDATED_AT: Final = datetime(2026, 2, 20, 14, 45, tzinfo=UTC)

SAMPLE_DELETED_AT: Final = datetime(2026, 3, 25, 18, 0, tzinfo=UTC)

REPLACEMENTS: Final = {
    "id": uuid4(),
    "name": "acme/hijacked",
    "created_at": datetime(1999, 1, 1, tzinfo=UTC),
    "updated_at": datetime(1999, 1, 1, tzinfo=UTC),
    "deleted_at": None,
}
"""A plausible-but-wrong value per field, used to attempt mutation.

Each differs from the sample entity's value, so a mutation that *succeeded*
would be observable rather than a no-op assignment that happens to look frozen.
"""


def make_repository(deleted_at: datetime | None = None) -> Repository:
    """Build a fully-populated entity with all five fields set.

    Args:
        deleted_at: Soft-deletion moment, or `None` for an active repository.

    Returns:
        A fresh instance; the entity is frozen, so no state leaks between tests.
    """
    return Repository(
        id=SAMPLE_ID,
        name="acme/backend-api",
        created_at=SAMPLE_CREATED_AT,
        updated_at=SAMPLE_UPDATED_AT,
        deleted_at=deleted_at,
    )


class BlockedImport(ImportError):
    """
    Raised when the entity module reaches for a framework it must not need.

    """


class FrameworkImportBlocker(MetaPathFinder):
    """A `sys.meta_path` finder that makes chosen top-level packages unimportable.

    Raising from `find_spec` — rather than returning `None` — is deliberate: a
    `None` would merely defer to the next finder, which would happily import the
    real package. Raising stops the import outright, so a forbidden import
    surfaces as a hard failure instead of a silent success.

    Attributes:
        blocked: Top-level package names to refuse. Submodules are refused too,
            matched on the part before the first dot.
    """

    def __init__(self, blocked: frozenset[str]) -> None:
        """Initialize the finder.

        Args:
            blocked: Top-level package names to refuse.
        """
        self.blocked = blocked

    def find_spec(
        self,
        fullname: str,
        path: Sequence[str] | None = None,
        target: ModuleType | None = None,
    ) -> ModuleSpec | None:
        """Refuse blocked packages, and defer on everything else.

        Args:
            fullname: The fully-qualified module name being imported.
            path: The parent package's search path, unused.
            target: The module being reloaded, unused.

        Returns:
            `None`, always — the return path is reached only for names that are
            not blocked, which the remaining finders handle normally.

        Raises:
            BlockedImport: If `fullname` names a blocked package or one of its
                submodules.
        """
        if fullname.partition(".")[0] in self.blocked:
            raise BlockedImport(f"{fullname!r} must not be importable from the domain layer")

        return None


def _blocked_module_names() -> list[str]:
    """List the currently-imported modules the blocker is responsible for.

    Returns:
        Every `sys.modules` key belonging to a blocked package, plus the entity
        module itself — which must be evicted so that importing it under the
        blocker actually re-executes its imports instead of returning the copy
        cached by an earlier test.
    """
    return [
        name
        for name in list(sys.modules)
        if name.partition(".")[0] in BLOCKED_FRAMEWORKS or name == ENTITY_MODULE
    ]


@contextmanager
def frameworks_made_unimportable() -> Iterator[None]:
    """Run a block with every framework package genuinely unimportable.

    The already-cached modules are evicted first, otherwise an `import fastapi`
    inside the block would be served straight from `sys.modules` and never reach
    the finder at all.

    Restoration puts the *original module objects* back, not freshly imported
    ones. Re-importing SQLAlchemy would give the rest of the suite a second set
    of class objects and a second mapper registry, breaking `isinstance` checks
    and ORM configuration in tests that run later in the same process.

    Yields:
        Control to the caller, with the blocker installed at the front of
        `sys.meta_path`.
    """
    evicted = {name: sys.modules[name] for name in _blocked_module_names()}
    for name in evicted:
        del sys.modules[name]

    blocker = FrameworkImportBlocker(BLOCKED_FRAMEWORKS)
    sys.meta_path.insert(0, blocker)

    try:
        yield
    finally:
        sys.meta_path.remove(blocker)

        for name in _blocked_module_names():
            del sys.modules[name]
        sys.modules.update(evicted)


# --------------------------------------------------------------------------- #
# Immutability
# --------------------------------------------------------------------------- #


def test_the_entity_carries_exactly_the_five_documented_fields() -> None:
    """Pins the field set the mutation tests iterate over.

    Without this, dropping a field from the entity would silently shrink the
    parametrization below to a smaller, still-green test suite.
    """
    assert FIELD_NAMES == ("id", "name", "created_at", "updated_at", "deleted_at")


@pytest.mark.parametrize("field_name", FIELD_NAMES)
def test_no_field_can_be_reassigned(field_name: str) -> None:
    """Every field is frozen, checked one field at a time.

    Testing all five separately, rather than one representative, is what catches
    a partial `__setattr__` override — a hand-written guard that names some
    fields and forgets others would still pass a single-field test.
    """
    repository = make_repository(deleted_at=SAMPLE_DELETED_AT)
    original = getattr(repository, field_name)
    replacement = REPLACEMENTS[field_name]
    assert replacement != original

    with pytest.raises(FrozenInstanceError):
        setattr(repository, field_name, replacement)

    assert getattr(repository, field_name) == original


@pytest.mark.parametrize("field_name", FIELD_NAMES)
def test_no_field_can_be_deleted(field_name: str) -> None:
    """Deletion is blocked as well as assignment.

    A frozen dataclass guards `__delattr__` too; a hand-rolled immutability
    shim that only guards `__setattr__` would leave an entity that can be
    hollowed out into an unusable half-object.
    """
    repository = make_repository(deleted_at=SAMPLE_DELETED_AT)

    with pytest.raises(FrozenInstanceError):
        delattr(repository, field_name)

    assert hasattr(repository, field_name)


def test_new_attributes_cannot_be_attached() -> None:
    """
    Immutability covers unknown names too, so no ad-hoc state can be smuggled on.

    """
    repository = make_repository()

    with pytest.raises(FrozenInstanceError):
        repository.tenant_id = "acme"  # type: ignore[attr-defined]

    assert not hasattr(repository, "tenant_id")


def test_a_derived_copy_leaves_the_original_untouched() -> None:
    """Immutability must not block deriving a new entity from an old one.

    The soft-delete path needs a "same repository, now deleted" value. This
    pins that `replace` produces it *without* touching the original — the
    property that makes passing entities around safe in the first place.
    """
    active = make_repository()

    deleted = replace(active, deleted_at=SAMPLE_DELETED_AT)

    assert deleted.deleted_at == SAMPLE_DELETED_AT
    assert active.deleted_at is None
    assert deleted.id == active.id


def test_entities_with_identical_fields_compare_equal() -> None:
    """Value equality lets tests assert on whole entities, not field by field.

    Every store and use-case test in this codebase compares returned entities
    directly, so losing `__eq__` would weaken those assertions to identity
    checks that can never pass across a persistence round trip.
    """
    assert make_repository() == make_repository()
    assert make_repository() != make_repository(deleted_at=SAMPLE_DELETED_AT)


# --------------------------------------------------------------------------- #
# Framework independence
# --------------------------------------------------------------------------- #


def test_the_import_blocker_actually_blocks() -> None:
    """Proves the harness has teeth before it is used to make a claim.

    A blocker that quietly failed — a wrong name match, a finder appended behind
    the real ones, a package served from a stale `sys.modules` entry — would
    make the test below pass no matter what the entity imported. This checks the
    blocker refuses a top-level package, a submodule, and a package that was
    definitely imported and cached beforehand.
    """
    assert "structlog" in sys.modules

    with frameworks_made_unimportable():
        for name in ("sqlalchemy", "fastapi", "pydantic", "starlette", "structlog"):
            with pytest.raises(ImportError):
                importlib.import_module(name)

        with pytest.raises(ImportError):
            importlib.import_module("sqlalchemy.ext.asyncio")

    assert "structlog" in sys.modules


def test_the_entity_imports_and_constructs_with_no_framework_available() -> None:
    """The domain layer stands up with no web framework and no ORM in scope.

    The module is evicted and re-imported inside the block, so its import
    statements are genuinely re-executed rather than served from cache; adding
    any framework import to `entity.py` turns this into a `BlockedImport`.
    Constructing an entity afterwards extends the check past import time to
    anything the class body defers until first use.
    """
    with frameworks_made_unimportable():
        assert ENTITY_MODULE not in sys.modules

        module = importlib.import_module(ENTITY_MODULE)

        entity = module.Repository(
            id=SAMPLE_ID,
            name="acme/backend-api",
            created_at=SAMPLE_CREATED_AT,
            updated_at=SAMPLE_UPDATED_AT,
            deleted_at=None,
        )

        assert entity.name == "acme/backend-api"
        assert entity.deleted_at is None
        assert module.Repository is not Repository


def test_the_framework_modules_are_restored_afterwards() -> None:
    """The blocker must not leave the interpreter poisoned for later tests.

    Re-importing SQLAlchemy instead of restoring the original object would give
    the process two copies of every mapped class, so this pins that the module
    objects come back by identity — the ordering-independence of every DB test
    that runs after this file depends on it.
    """
    before = {name: sys.modules[name] for name in _blocked_module_names()}

    with frameworks_made_unimportable():
        pass

    after = {name: sys.modules[name] for name in _blocked_module_names()}

    assert after.keys() == before.keys()
    assert all(after[name] is before[name] for name in before)
