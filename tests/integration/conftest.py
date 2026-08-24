"""
Fixtures shared by every integration test module.

"""

from collections.abc import Iterator

import pytest

from wooloo.main import app


@pytest.fixture(autouse=True)
def clean_dependency_overrides() -> Iterator[None]:
    """
    Guarantee each test starts and ends with an unmodified application.

    """
    app.dependency_overrides.clear()
    yield
    app.dependency_overrides.clear()
