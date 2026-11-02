#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["questionary>=2.0", "pyyaml>=6.0"]
# ///
"""Interactive generator for the TrueNAS "Install via YAML" app definition.

Run with:  uv run truenas/generate_yaml.py [--output FILE]
"""

from __future__ import annotations

import argparse
import ipaddress
import os
import secrets
import sys
from pathlib import Path
from urllib.parse import urlparse

import questionary

from compose_builder import (
    CONTAINER_PORT,
    DEFAULT_PACKAGES,
    AppConfig,
    Secrets,
    Storage,
    build_compose,
    dump_yaml,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCKERFILE = REPO_ROOT / "Dockerfile"
ENTRYPOINT = REPO_ROOT / "docker-entrypoint.sh"
DEFAULT_OUTPUT = Path("openclaw-truenas.yaml")


class Cancelled(Exception):
    """Raised when the user aborts a prompt (Ctrl-C / Esc)."""


def ask(question: questionary.Question):
    answer = question.ask()
    if answer is None:
        raise Cancelled
    return answer


# --- validators: return True or an error message ---------------------------


def validate_port(value: str) -> bool | str:
    if value.isdigit() and 1 <= int(value) <= 65535:
        return True
    return "Enter a port between 1 and 65535"


def validate_ip(value: str) -> bool | str:
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return "Enter a valid IPv4/IPv6 address (0.0.0.0 = all interfaces)"
    return True


def validate_hosts(value: str) -> bool | str:
    hosts = split_list(value)
    if not hosts:
        return "Enter at least one hostname or IP"
    for host in hosts:
        if "/" in host or (":" in host and not _is_ipv6(host)):
            return f"Enter bare hostnames or IPs (no scheme, port or path): {host}"
    return True


def validate_urls(value: str) -> bool | str:
    for url in split_list(value):
        parsed = urlparse(url)
        if (
            parsed.scheme not in ("http", "https")
            or not parsed.netloc
            or parsed.path not in ("", "/")
        ):
            return f"Not an origin URL (scheme://host[:port]): {url}"
    return True


def validate_dataset_path(value: str) -> bool | str:
    if value.startswith("/mnt/") and len(value.rstrip("/")) > len("/mnt"):
        return True
    return "TrueNAS datasets live under /mnt/<pool>/..."


def validate_required(value: str) -> bool | str:
    return True if value.strip() else "This value is required"


def validate_image(value: str) -> bool | str:
    if value.strip() and " " not in value.strip():
        return True
    return "Enter an image reference, e.g. ghcr.io/user/truenas-openclaw:latest"


def _is_ipv6(host: str) -> bool:
    try:
        return ipaddress.ip_address(host.strip("[]")).version == 6
    except ValueError:
        return False


def split_list(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


# --- helpers -----------------------------------------------------------------


def format_host(host: str) -> str:
    return f"[{host.strip('[]')}]" if _is_ipv6(host) else host


def build_origins(hosts: list[str], port: int, extra: list[str]) -> tuple[str, ...]:
    origins = [f"http://{format_host(host)}:{port}" for host in hosts]
    origins += [f"http://localhost:{port}", f"http://127.0.0.1:{port}"]
    origins += [url.rstrip("/") for url in extra]
    return tuple(dict.fromkeys(origins))  # de-duplicate, keep order


def local_timezone() -> str:
    if os.environ.get("TZ"):
        return os.environ["TZ"]
    localtime = Path("/etc/localtime")
    if localtime.is_symlink():
        target = str(localtime.resolve())
        if "zoneinfo/" in target:
            return target.split("zoneinfo/", 1)[1]
    return "UTC"


def read_source(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as err:
        raise SystemExit(f"Cannot read {path}: {err}") from err


# --- prompt sections ---------------------------------------------------------


def prompt_image() -> tuple[str, str]:
    questionary.print("\n== Image ==", style="bold")
    source = ask(
        questionary.select(
            "Where should TrueNAS get the image from?",
            choices=[
                questionary.Choice(
                    "Build on TrueNAS from the embedded Dockerfile (recommended)",
                    "inline",
                ),
                questionary.Choice("Pull a prebuilt image from a registry", "prebuilt"),
            ],
        )
    )
    image = ""
    if source == "prebuilt":
        image = ask(
            questionary.text("Image reference:", validate=validate_image)
        ).strip()
    return source, image


def prompt_network() -> dict:
    questionary.print("\n== Network ==", style="bold")
    mode = ask(
        questionary.select(
            "Network mode:",
            choices=[
                questionary.Choice(
                    "Bridge, publish the gateway port (recommended)", "bridge"
                ),
                questionary.Choice(
                    "Host network (container uses the TrueNAS network stack)", "host"
                ),
            ],
        )
    )
    port = int(
        ask(
            questionary.text(
                "Gateway port on TrueNAS:",
                default=str(CONTAINER_PORT),
                validate=validate_port,
            )
        )
    )
    bind_address = "0.0.0.0"
    if mode == "bridge":
        bind_address = ask(
            questionary.text(
                "Bind address (0.0.0.0 = all interfaces):",
                default="0.0.0.0",
                validate=validate_ip,
            )
        )
    hosts = split_list(
        ask(
            questionary.text(
                "TrueNAS hostnames/IPs used to open the Control UI (comma-separated):",
                default="truenas.local",
                validate=validate_hosts,
            )
        )
    )
    extra = split_list(
        ask(
            questionary.text(
                "Extra allowed origins, e.g. a reverse proxy https://openclaw.example.com (optional):",
                validate=validate_urls,
            )
        )
    )
    return {
        "network_mode": mode,
        "host_port": port,
        "bind_address": bind_address,
        "control_ui_origins": build_origins(hosts, port, extra),
    }


def prompt_storage() -> Storage:
    questionary.print("\n== Storage ==", style="bold")
    kind = ask(
        questionary.select(
            "Where should packages, config and credentials be stored?",
            choices=[
                questionary.Choice(
                    "Host paths on a dataset (recommended, easy to back up)", "host"
                ),
                questionary.Choice(
                    "Docker named volumes (managed by TrueNAS)", "volumes"
                ),
            ],
        )
    )
    if kind == "volumes":
        return Storage("volumes")
    base = ask(
        questionary.text(
            "Base dataset path (subfolders tools/openclaw/claude/gemini are created):",
            default="/mnt/tank/apps/openclaw",
            validate=validate_dataset_path,
        )
    )
    return Storage("host", base.rstrip("/"))


def prompt_environment() -> dict:
    questionary.print("\n== Environment ==", style="bold")
    if ask(questionary.confirm("Generate a random gateway token?", default=True)):
        token = secrets.token_hex(32)
    else:
        token = ask(questionary.password("Gateway token:", validate=validate_required))
    anthropic = ask(
        questionary.password(
            "ANTHROPIC_API_KEY (optional, empty = log in with `claude` later):"
        )
    )
    antigravity = ask(
        questionary.password(
            "ANTIGRAVITY_API_KEY (optional, empty = log in with `agy` later):"
        )
    )
    timezone = ask(
        questionary.text(
            "Timezone:", default=local_timezone(), validate=validate_required
        )
    )
    packages = ask(
        questionary.text(
            "Global pnpm packages:",
            default=DEFAULT_PACKAGES,
            validate=validate_required,
        )
    )
    skip_update = ask(
        questionary.confirm(
            "Skip package updates on start (only install missing ones)?", default=False
        )
    )
    return {
        "secrets": Secrets(token, anthropic.strip(), antigravity.strip()),
        "timezone": timezone.strip(),
        "packages": " ".join(packages.split()),
        "skip_update": skip_update,
    }


def print_summary(config: AppConfig) -> None:
    storage = (
        f"host paths under {config.storage.base_path}"
        if config.storage.kind == "host"
        else "named volumes"
    )
    network = (
        f"bridge, {config.bind_address}:{config.host_port} -> {CONTAINER_PORT}"
        if config.network_mode == "bridge"
        else f"host, port {config.host_port}"
    )
    lines = [
        f"Image:       {config.image} ({config.image_source})",
        f"Network:     {network}",
        f"Origins:     {', '.join(config.control_ui_origins)}",
        f"Storage:     {storage}",
        f"Timezone:    {config.timezone}",
        f"Packages:    {config.packages}",
        f"Skip update: {'yes' if config.skip_update else 'no'}",
        (
            f"API keys:    Anthropic {'set' if config.secrets.anthropic_api_key else 'not set'}, "
            f"Antigravity {'set' if config.secrets.antigravity_api_key else 'not set'}"
        ),
    ]
    questionary.print("\n== Summary ==", style="bold")
    questionary.print("\n".join(lines))


def write_output(text: str, output: Path) -> None:
    # The YAML contains the gateway token and API keys: owner-only permissions.
    fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)


def run(output: Path) -> int:
    dockerfile = read_source(DOCKERFILE)
    entrypoint = read_source(ENTRYPOINT)

    questionary.print("OpenClaw TrueNAS YAML generator", style="bold fg:ansicyan")
    image_source, prebuilt_image = prompt_image()
    network = prompt_network()
    storage = prompt_storage()
    env = prompt_environment()

    config = AppConfig(
        image_source=image_source,
        prebuilt_image=prebuilt_image,
        storage=storage,
        **network,
        **env,
    )
    print_summary(config)
    if not ask(questionary.confirm("Write the YAML?", default=True)):
        return 1

    text = dump_yaml(build_compose(config, dockerfile, entrypoint))
    write_output(text, output)
    questionary.print(
        f"\nWrote {output.resolve()} (mode 600, contains secrets).",
        style="fg:ansigreen",
    )
    if ask(questionary.confirm("Show the gateway token now?", default=True)):
        questionary.print(f"Gateway token: {config.secrets.gateway_token}")
    if ask(questionary.confirm("Print the YAML to paste into TrueNAS?", default=False)):
        print(text)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help="output file (default: %(default)s)",
    )
    args = parser.parse_args()
    try:
        return run(args.output)
    except Cancelled:
        print("Cancelled.", file=sys.stderr)
        return 130
    except ValueError as err:
        print(f"Error: {err}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
