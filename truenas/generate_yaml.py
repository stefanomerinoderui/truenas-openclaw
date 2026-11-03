#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["textual>=8.0", "pyyaml>=6.0", "python-dotenv>=1.0"]
# ///
"""Full-screen editor that generates the TrueNAS "Install via YAML" app definition.

New config:       uv run truenas/generate_yaml.py
Edit a config:    uv run truenas/generate_yaml.py --config openclaw-truenas.json
Other env file:   uv run truenas/generate_yaml.py --env-file ~/secrets.env

The JSON config holds every setting except secrets. The gateway token and API
keys are read from the env file (default: ./.env) or the environment, can be
edited in the Secrets section, and are never written to the config.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from draft import load_warnings
from envfile import EnvFileError, load_secret_values, resolve_env_file
from settings import ConfigError, default_settings_dict, read_config_json
from tui import GeneratorApp

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCKERFILE = REPO_ROOT / "Dockerfile"
ENTRYPOINT = REPO_ROOT / "docker-entrypoint.sh"
DEFAULT_OUTPUT = Path("openclaw-truenas.yaml")


def read_source(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as err:
        raise SystemExit(f"Cannot read {path}: {err}") from err


def read_sources() -> tuple[str, str]:
    return read_source(DOCKERFILE), read_source(ENTRYPOINT)


def initial_config(path: Path | None) -> tuple[dict[str, Any], list[str]]:
    """The config to open and the settings it contains that will be dropped.

    No path, or a path that doesn't exist yet, starts from the defaults (a new
    config is saved there). An existing file is opened even when its values are
    invalid, so they can be fixed in the editor; it only has to be a JSON object.
    """
    if path is None or not path.exists():
        return default_settings_dict(), []
    data = read_config_json(path)
    if not isinstance(data, dict):
        raise ConfigError([("", f"{path} must contain a JSON object")])
    return data, load_warnings(data)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "-c",
        "--config",
        type=Path,
        help="JSON config to edit; created on first save if it doesn't exist",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        help=(
            "dotenv file with OPENCLAW_GATEWAY_TOKEN, ANTHROPIC_API_KEY, "
            "ANTIGRAVITY_API_KEY and OPENCLAW_GATEWAY_PASSWORD "
            "(default: ./.env if it exists)"
        ),
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help="YAML output file, can be changed in the editor (default: %(default)s)",
    )
    args = parser.parse_args(argv)

    try:
        env_file = resolve_env_file(args.env_file)
        known = load_secret_values(env_file)
        data, warnings = initial_config(args.config)
    except ConfigError as err:
        print(f"Cannot open the config:\n{err}", file=sys.stderr)
        return 1
    except EnvFileError as err:
        print(f"Error: {err}", file=sys.stderr)
        return 1
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        print("Error: the editor needs an interactive terminal.", file=sys.stderr)
        return 1

    GeneratorApp(
        data=data,
        config_path=args.config,
        output=args.output,
        known=known,
        env_file=env_file,
        sources=read_sources(),
        warnings=warnings,
    ).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
