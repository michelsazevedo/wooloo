"""
Unit tests proving `domain/storage/` depends on no framework and no backend.

"""

import importlib
import sys
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from importlib.abc import MetaPathFinder
from importlib.machinery import ModuleSpec
from types import ModuleType
from typing import Final

import pytest

from wooloo.domain.storage.exceptions import BlobNotFound, StorageException
from wooloo.domain.storage.models import StoredBlob

STORAGE_PACKAGE: Final = "wooloo.domain.storage"

STORAGE_MODULES: Final = (
    f"{STORAGE_PACKAGE}.models",
    f"{STORAGE_PACKAGE}.exceptions",
    f"{STORAGE_PACKAGE}.contracts",
)

BLOCKED_ROOTS: Final = frozenset(
    {
        "fastapi",
        "sqlalchemy",
        "pydantic",
        "starlette",
        "structlog",
        "boto3",
        "os",
        "pathlib",
        "shutil",
        "tempfile",
    }
)

class BlockedImport(ImportError):
    """
    Raised when a storage domain module reaches for a package it must not need.

    """


class DependencyImportBlocker(MetaPathFinder):
    """A `sys.meta_path` finder that makes chosen top-level packages unimportable.

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


def _managed_module_names() -> list[str]:
    """List the currently-imported modules the blocker is responsible for.

    Returns:
        Every `sys.modules` key belonging to a blocked package, plus the storage
        package and its submodules — which must be evicted so that importing
        them under the blocker actually re-executes their imports instead of
        returning the copies cached by an earlier test.
    """
    return [
        name
        for name in list(sys.modules)
        if name.partition(".")[0] in BLOCKED_ROOTS
        or name == STORAGE_PACKAGE
        or name.startswith(f"{STORAGE_PACKAGE}.")
    ]


@contextmanager
def dependencies_made_unimportable() -> Iterator[None]:
    """Run a block with every forbidden package genuinely unimportable.

    Yields:
        Control to the caller, with the blocker installed at the front of
        `sys.meta_path`.
    """
    evicted = {name: sys.modules[name] for name in _managed_module_names()}
    for name in evicted:
        del sys.modules[name]

    blocker = DependencyImportBlocker(BLOCKED_ROOTS)
    sys.meta_path.insert(0, blocker)

    try:
        yield
    finally:
        sys.meta_path.remove(blocker)

        for name in _managed_module_names():
            del sys.modules[name]
        sys.modules.update(evicted)


def test_the_import_blocker_actually_blocks() -> None:
    """
    Proves the harness has teeth before it is used to make a claim.

    """
    assert "sqlalchemy" in sys.modules
    assert "os" in sys.modules

    with dependencies_made_unimportable():
        for name in sorted(BLOCKED_ROOTS):
            with pytest.raises(BlockedImport):
                importlib.import_module(name)

        for submodule in ("sqlalchemy.ext.asyncio", "os.path"):
            with pytest.raises(BlockedImport):
                importlib.import_module(submodule)

    assert "sqlalchemy" in sys.modules
    assert "os" in sys.modules


def test_the_storage_domain_imports_and_works_with_everything_blocked() -> None:
    """
    All three storage domain modules stand up with nothing forbidden in scope.

    """
    with dependencies_made_unimportable():
        for name in (STORAGE_PACKAGE, *STORAGE_MODULES):
            assert name not in sys.modules

        models = importlib.import_module(STORAGE_MODULES[0])
        exceptions = importlib.import_module(STORAGE_MODULES[1])
        contracts = importlib.import_module(STORAGE_MODULES[2])

        blob = models.StoredBlob(
            key=f"sha256:{'ab' * 32}",
            size=4096,
            content_type=None,
            created_at=datetime(2026, 5, 14, 11, 20, tzinfo=UTC),
        )
        assert blob.size == 4096
        assert models.StoredBlob is not StoredBlob

        with pytest.raises(exceptions.StorageException) as raised:
            raise exceptions.BlobNotFound("unknown key")

        assert str(raised.value) == "unknown key"
        assert exceptions.BlobNotFound is not BlobNotFound
        assert exceptions.StorageException is not StorageException

        assert callable(contracts.BlobStorage.put)
        assert contracts.StoredBlob is models.StoredBlob


def test_the_blocked_modules_are_restored_afterwards() -> None:
    """
    The blocker must not leave the interpreter poisoned for later tests.

    """
    before = {name: sys.modules[name] for name in _managed_module_names()}

    with dependencies_made_unimportable():
        pass

    after = {name: sys.modules[name] for name in _managed_module_names()}

    assert after.keys() == before.keys()
    assert all(after[name] is before[name] for name in before)
