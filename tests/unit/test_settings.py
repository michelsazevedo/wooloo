"""
Unit tests for the settings module's configuration sources and validation.

"""

from pathlib import Path

import pytest
from pydantic import ValidationError

from wooloo.config.settings import Settings

# test_settings.py -> unit -> tests -> repository root.
EXPECTED_REPO_ROOT = Path(__file__).resolve().parents[2]

EXPECTED_ENV_FILE = EXPECTED_REPO_ROOT / ".env"

STUB_DATABASE_URL = "postgresql+asyncpg://user:password@localhost:5432/wooloo"

NORMALISED_LOG_LEVELS = [
    ("DEBUG", "DEBUG"),
    ("debug", "DEBUG"),
    ("Info", "INFO"),
    ("  Warning  ", "WARNING"),
    ("INFO\n", "INFO"),
    ("\terror\t", "ERROR"),
    ("critical", "CRITICAL"),
]

UNUSABLE_LOG_LEVELS = [
    "",
    "   ",
    "\n",
    "verbose",
    "WARN",
    "TRACE",
    "NOTSET",
    "INFO,DEBUG",
]


def test_env_file_is_anchored_to_the_repository_root() -> None:
    """
    Configuration must resolve identically from every working directory.

    """
    env_file = Settings.model_config["env_file"]

    assert isinstance(env_file, Path)
    assert env_file.is_absolute()
    assert env_file == EXPECTED_ENV_FILE


def test_log_level_defaults_to_info_when_unconfigured() -> None:
    """
    An operator who sets nothing must get a usable, quiet-enough default.

    """
    settings = Settings(database_url=STUB_DATABASE_URL)

    assert settings.log_level == "INFO"


@pytest.mark.parametrize(("configured", "expected"), NORMALISED_LOG_LEVELS, ids=repr)
def test_log_level_is_normalised_to_the_canonical_upper_case_name(
    configured: str, expected: str
) -> None:
    """
    However an operator types the level, the application must see one spelling.

    Args:
        configured: The raw value as an operator might type it.
        expected: The canonical name the rest of the application must receive.
    """
    settings = Settings(database_url=STUB_DATABASE_URL, log_level=configured)

    assert settings.log_level == expected


@pytest.mark.parametrize("configured", UNUSABLE_LOG_LEVELS, ids=repr)
def test_an_unusable_log_level_fails_fast_at_construction(configured: str) -> None:
    """
    A level the application cannot honour must stop the process, not be ignored.

    Args:
        configured: A value the application must refuse.
    """
    with pytest.raises(ValidationError) as raised:
        Settings(database_url=STUB_DATABASE_URL, log_level=configured)

    assert "log_level" in str(raised.value)
