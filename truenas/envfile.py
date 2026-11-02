"""Load the secrets (gateway token, API keys) from an env file and the environment.

Secrets are deliberately kept out of the JSON config so that it can be shared
and committed; they come from a dotenv-style file or from real environment
variables instead. Only the variables listed in ``SECRET_VARIABLES`` are read.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from dotenv import dotenv_values

GATEWAY_TOKEN_VAR = "OPENCLAW_GATEWAY_TOKEN"
ANTHROPIC_KEY_VAR = "ANTHROPIC_API_KEY"
ANTIGRAVITY_KEY_VAR = "ANTIGRAVITY_API_KEY"
GATEWAY_PASSWORD_VAR = "OPENCLAW_GATEWAY_PASSWORD"
SECRET_VARIABLES = (
    GATEWAY_TOKEN_VAR,
    ANTHROPIC_KEY_VAR,
    ANTIGRAVITY_KEY_VAR,
    GATEWAY_PASSWORD_VAR,
)
DEFAULT_ENV_FILE = Path(".env")


class EnvFileError(Exception):
    """Raised when the requested env file cannot be read."""


def resolve_env_file(requested: Path | None) -> Path | None:
    """Use the requested file, else ./.env when it exists, else none."""
    if requested is not None:
        return requested
    return DEFAULT_ENV_FILE if DEFAULT_ENV_FILE.is_file() else None


def load_secret_values(
    env_file: Path | None, environ: Mapping[str, str] | None = None
) -> dict[str, str]:
    """Return the non-empty secrets found; real environment variables win."""
    environ = os.environ if environ is None else environ
    from_file: Mapping[str, str | None] = {}
    if env_file is not None:
        if not env_file.is_file():
            raise EnvFileError(f"env file not found: {env_file}")
        try:
            from_file = dotenv_values(env_file)
        except OSError as err:
            raise EnvFileError(f"cannot read {env_file}: {err}") from err

    values = {}
    for name in SECRET_VARIABLES:
        value = environ.get(name) or from_file.get(name)
        if value:
            values[name] = value
    return values
