"""Unit tests for the settings module's configuration sources.

The value under test is the ``env_file`` path itself, not a loaded setting, so
nothing here reads the environment or the real ``.env``: the assertions inspect
`Settings.model_config` on the class.

Independent derivation
----------------------
The expected path is rebuilt from *this file's* location rather than imported from
`settings._REPO_ROOT`. Comparing the module's constant to itself would pass no
matter how many directory levels it walked up; deriving the answer from a second,
differently-rooted starting point is what gives the test authority over the
constant instead of agreement with it.
"""

from pathlib import Path

from wooloo.config.settings import Settings

# test_settings.py -> unit -> tests -> repository root.
EXPECTED_REPO_ROOT = Path(__file__).resolve().parents[2]

EXPECTED_ENV_FILE = EXPECTED_REPO_ROOT / ".env"


def test_env_file_is_anchored_to_the_repository_root() -> None:
    """Configuration must resolve identically from every working directory.

    pydantic-settings resolves a relative ``env_file`` against the process working
    directory. A bare ``".env"`` therefore makes configuration depend on where the
    server was launched from: starting it outside the repository root either raises
    `ValidationError` for a setting that is in fact configured, or silently loads an
    unrelated ``.env`` that happens to sit in that directory — a config source the
    operator never intended, with no error to signal it.

    The `isinstance` assertion is load-bearing rather than a type-narrowing
    formality: a reversion to the bare string ``".env"`` fails here first.
    """
    env_file = Settings.model_config["env_file"]

    assert isinstance(env_file, Path)
    assert env_file.is_absolute()
    assert env_file == EXPECTED_ENV_FILE
