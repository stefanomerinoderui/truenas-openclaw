#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["questionary>=2.0", "pyyaml>=6.0", "python-dotenv>=1.0"]
# ///
"""Generator for the TrueNAS "Install via YAML" app definition.

Interactive:   uv run truenas/generate_yaml.py [-o FILE]
From a config: uv run truenas/generate_yaml.py --config FILE [--env-file FILE]

The JSON config holds every setting except secrets, and skips all prompts. The
gateway token and API keys are read from the env file (default: ./.env) or the
environment, never from the JSON.
"""

from __future__ import annotations

import argparse
import os
import secrets
import sys
from pathlib import Path

import questionary

from clipboard import ClipboardError, copy_to_clipboard, detect_clipboard
from compose_builder import (
    CONTAINER_PORT,
    DEFAULT_IMAGE,
    DEFAULT_PACKAGES,
    AppConfig,
    ExtraNetwork,
    Secrets,
    Storage,
    TrustedProxy,
    build_compose,
    dump_yaml,
    format_host,
)
from envfile import (
    ANTHROPIC_KEY_VAR,
    ANTIGRAVITY_KEY_VAR,
    GATEWAY_PASSWORD_VAR,
    GATEWAY_TOKEN_VAR,
    EnvFileError,
    load_secret_values,
    resolve_env_file,
)
from settings import (
    DEFAULT_HOSTS,
    DEFAULT_PROXY_HOSTNAME,
    DEFAULT_PROXY_NETWORK,
    DEFAULT_PUBLIC_URL,
    DEFAULT_STORAGE_PATH,
    ConfigError,
    Settings,
    build_app_config,
    default_timezone,
    load_settings,
    save_settings,
)
from validators import (
    parse_bind_addresses,
    split_list,
    validate_bind_addresses,
    validate_dataset_path,
    validate_header_name,
    validate_header_names,
    validate_hostname,
    validate_hosts,
    validate_identities,
    validate_image,
    validate_network_name,
    validate_port,
    validate_required,
    validate_required_urls,
    validate_trusted_proxies,
    validate_urls,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCKERFILE = REPO_ROOT / "Dockerfile"
ENTRYPOINT = REPO_ROOT / "docker-entrypoint.sh"
DEFAULT_OUTPUT = Path("openclaw-truenas.yaml")
DEFAULT_CONFIG = Path("openclaw-truenas.json")
DEFAULT_USER_HEADER = "x-forwarded-user"
DEFAULT_REQUIRED_HEADERS = "x-forwarded-proto, x-forwarded-host"


class Cancelled(Exception):
    """Raised when the user aborts a prompt (Ctrl-C / Esc)."""


def ask(question: questionary.Question):
    answer = question.ask()
    if answer is None:
        raise Cancelled
    return answer


def read_source(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as err:
        raise SystemExit(f"Cannot read {path}: {err}") from err


# --- prompt sections ---------------------------------------------------------


def prompt_image() -> dict:
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
            questionary.text(
                "Image reference:", default=DEFAULT_IMAGE, validate=validate_image
            )
        ).strip()
    return {"image_source": source, "prebuilt_image": image}


def prompt_network_settings() -> ExtraNetwork:
    """Ask which existing Docker network to join and under which hostname."""
    name = ask(
        questionary.text(
            "Existing network name:",
            default=DEFAULT_PROXY_NETWORK,
            validate=validate_network_name,
        )
    )
    hostname = ask(
        questionary.text(
            "Hostname of the openclaw container on that network:",
            default=DEFAULT_PROXY_HOSTNAME,
            validate=validate_hostname,
        )
    )
    return ExtraNetwork(name.strip(), hostname.strip())


def prompt_extra_network() -> ExtraNetwork | None:
    """Optionally join an existing Docker network on top of the published port."""
    if not ask(
        questionary.confirm(
            "Also connect to an existing Docker network (e.g. a reverse proxy)?",
            default=False,
        )
    ):
        return None
    return prompt_network_settings()


def prompt_proxy_mode() -> dict:
    """Gateway reachable only through a reverse proxy: no published ports."""
    proxy_network = prompt_network_settings()
    urls = split_list(
        ask(
            questionary.text(
                "Public URL(s) served by the proxy (comma-separated):",
                default=DEFAULT_PUBLIC_URL,
                validate=validate_required_urls,
            )
        )
    )
    return {
        "network_mode": "proxy",
        "proxy_network": proxy_network,
        "public_urls": tuple(urls),
    }


def prompt_published_port_mode(mode: str) -> dict:
    """Bridge or host network: the gateway port is reachable on TrueNAS itself."""
    port = int(
        ask(
            questionary.text(
                "Gateway port on TrueNAS:",
                default=str(CONTAINER_PORT),
                validate=validate_port,
            )
        )
    )
    bind_addresses = ("0.0.0.0",)
    proxy_network = None
    if mode == "bridge":
        bind_addresses = parse_bind_addresses(
            ask(
                questionary.text(
                    "Bind addresses (comma-separated, 0.0.0.0 = all interfaces):",
                    default="0.0.0.0",
                    validate=validate_bind_addresses,
                )
            )
        )
        proxy_network = prompt_extra_network()
    hosts = split_list(
        ask(
            questionary.text(
                "TrueNAS hostnames/IPs used to open the Control UI (comma-separated):",
                default=DEFAULT_HOSTS,
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
        "port": port,
        "bind_addresses": bind_addresses,
        "proxy_network": proxy_network,
        "hosts": tuple(hosts),
        "extra_origins": tuple(extra),
    }


def prompt_network() -> dict:
    questionary.print("\n== Network ==", style="bold")
    mode = ask(
        questionary.select(
            "Network mode:",
            choices=[
                questionary.Choice(
                    "Behind a reverse proxy: join its Docker network, no published ports (recommended)",
                    "proxy",
                ),
                questionary.Choice(
                    "Bridge, publish the gateway port on TrueNAS", "bridge"
                ),
                questionary.Choice(
                    "Host network (container uses the TrueNAS network stack)", "host"
                ),
            ],
        )
    )
    if mode == "proxy":
        return prompt_proxy_mode()
    return prompt_published_port_mode(mode)


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
            default=DEFAULT_STORAGE_PATH,
            validate=validate_dataset_path,
        )
    )
    return Storage("host", base.rstrip("/"))


def prompt_environment() -> dict:
    questionary.print("\n== Environment ==", style="bold")
    timezone = ask(
        questionary.text(
            "Timezone:", default=default_timezone(), validate=validate_required
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
        "timezone": timezone.strip(),
        "packages": tuple(packages.split()),
        "skip_update": skip_update,
    }


def prompt_trusted_proxy() -> TrustedProxy | None:
    """Optionally let the reverse proxy authenticate users instead of a token."""
    questionary.print("\n== Authentication ==", style="bold")
    if not ask(
        questionary.confirm(
            "Delegate authentication to the reverse proxy (trusted-proxy auth) "
            "instead of a gateway token?",
            default=False,
        )
    ):
        return None
    questionary.print(
        "Needs a proxy that authenticates users and forwards their identity in a "
        "header (e.g. oauth2-proxy, Pomerium), overwriting that header and "
        "X-Forwarded-* instead of appending to them.\n"
        "Find the proxy network's subnet with: sudo docker network inspect <network>"
    )
    proxies = split_list(
        ask(
            questionary.text(
                "Trusted proxy IPs/CIDRs (comma-separated, e.g. the proxy network's subnet):",
                validate=validate_trusted_proxies,
            )
        )
    )
    user_header = ask(
        questionary.text(
            "Header with the authenticated user "
            "(x-auth-request-email for oauth2-proxy, x-pomerium-claim-email for Pomerium):",
            default=DEFAULT_USER_HEADER,
            validate=validate_header_name,
        )
    )
    required_headers = split_list(
        ask(
            questionary.text(
                "Headers the proxy must always send (comma-separated, optional):",
                default=DEFAULT_REQUIRED_HEADERS,
                validate=validate_header_names,
            )
        )
    )
    allow_users = split_list(
        ask(
            questionary.text(
                "Allowed users (comma-separated; empty = every user the proxy authenticates):",
                validate=validate_identities,
            )
        )
    )
    return TrustedProxy(
        proxies=tuple(dict.fromkeys(proxies)),
        user_header=user_header.strip().lower(),
        required_headers=tuple(dict.fromkeys(h.lower() for h in required_headers)),
        allow_users=tuple(dict.fromkeys(allow_users)),
    )


def prompt_settings() -> Settings:
    image = prompt_image()
    network = prompt_network()
    trusted_proxy = prompt_trusted_proxy()
    storage = prompt_storage()
    environment = prompt_environment()
    return Settings(
        storage=storage,
        trusted_proxy=trusted_proxy,
        **image,
        **network,
        **environment,
    )


def warn_ignored_secrets(known: dict[str, str], trusted_proxy: bool) -> None:
    """Tell the user about loaded secrets that don't apply to the chosen auth."""
    if trusted_proxy and GATEWAY_TOKEN_VAR in known:
        reason = (
            f"{GATEWAY_TOKEN_VAR} ignored: the gateway refuses to start with "
            "trusted-proxy auth and a shared token."
        )
    elif not trusted_proxy and GATEWAY_PASSWORD_VAR in known:
        reason = (
            f"{GATEWAY_PASSWORD_VAR} ignored: it is only used with trusted-proxy auth."
        )
    else:
        return
    questionary.print(reason, style="fg:ansiyellow")


def prompt_gateway_token(known: dict[str, str]) -> tuple[str, bool]:
    """Return the gateway token and whether it was generated here."""
    if known.get(GATEWAY_TOKEN_VAR):
        return known[GATEWAY_TOKEN_VAR], False
    if ask(questionary.confirm("Generate a random gateway token?", default=True)):
        return secrets.token_hex(32), True
    return ask(
        questionary.password("Gateway token:", validate=validate_required)
    ), False


def prompt_secrets(known: dict[str, str], trusted_proxy: bool) -> tuple[Secrets, bool]:
    """Ask only for the secrets the env file / environment did not provide."""
    questionary.print("\n== Secrets (never saved to the JSON config) ==", style="bold")
    warn_ignored_secrets(known, trusted_proxy)
    token, token_generated, password = "", False, ""
    if trusted_proxy:
        password = known.get(GATEWAY_PASSWORD_VAR) or ask(
            questionary.password(
                f"{GATEWAY_PASSWORD_VAR} (optional, lets internal clients bypass the proxy):"
            )
        )
    else:
        token, token_generated = prompt_gateway_token(known)
    anthropic = known.get(ANTHROPIC_KEY_VAR) or ask(
        questionary.password(
            f"{ANTHROPIC_KEY_VAR} (optional, empty = log in with `claude` later):"
        )
    )
    antigravity = known.get(ANTIGRAVITY_KEY_VAR) or ask(
        questionary.password(
            f"{ANTIGRAVITY_KEY_VAR} (optional, empty = log in with `agy` later):"
        )
    )
    return (
        Secrets(token, anthropic.strip(), antigravity.strip(), password.strip()),
        token_generated,
    )


def print_summary(config: AppConfig) -> None:
    storage = (
        f"host paths under {config.storage.base_path}"
        if config.storage.kind == "host"
        else "named volumes"
    )
    bindings = ", ".join(
        f"{format_host(address)}:{config.host_port}"
        for address in config.bind_addresses
    )
    network = {
        "proxy": "proxy only, no published ports",
        "bridge": f"bridge, {bindings} -> {CONTAINER_PORT}",
        "host": f"host, port {config.host_port}",
    }[config.network_mode]
    extra = config.extra_network
    proxy_network = (
        f"{extra.name} (hostname {extra.hostname}, port {CONTAINER_PORT})"
        if extra
        else "none"
    )
    proxy = config.trusted_proxy
    auth = (
        "gateway token"
        if proxy is None
        else f"trusted proxy ({proxy.user_header} header from "
        f"{', '.join(proxy.proxies)}; users: "
        f"{', '.join(proxy.allow_users) or 'any authenticated user'})"
    )
    lines = [
        f"Image:       {config.image} ({config.image_source})",
        f"Network:     {network}",
        f"Joined net:  {proxy_network}",
        f"Auth:        {auth}",
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


def write_yaml(
    config: AppConfig, output: Path, dockerfile: str, entrypoint: str
) -> str:
    text = dump_yaml(build_compose(config, dockerfile, entrypoint))
    write_output(text, output)
    questionary.print(
        f"\nWrote {output.resolve()} (mode 600, contains secrets).",
        style="fg:ansigreen",
    )
    return text


def offer_token(token: str, generated: bool) -> None:
    """Show a generated token; a token the user typed in needs no echo."""
    if generated and ask(
        questionary.confirm("Show the gateway token now?", default=True)
    ):
        questionary.print(f"Gateway token: {token}")


def offer_config_save(settings: Settings) -> None:
    """Offer to keep the answers as a reusable JSON config (secrets excluded)."""
    if not ask(
        questionary.confirm(
            "Save these settings as a JSON config (secrets are never included)?",
            default=True,
        )
    ):
        return
    path = Path(
        ask(
            questionary.text(
                "Config file:", default=str(DEFAULT_CONFIG), validate=validate_required
            )
        ).strip()
    )
    if path.exists() and not ask(
        questionary.confirm(f"{path} already exists. Overwrite?", default=False)
    ):
        questionary.print("Config not saved.", style="fg:ansiyellow")
        return
    try:
        save_settings(settings, path)
    except OSError as err:
        questionary.print(f"Could not save {path}: {err}", style="fg:ansired")
        return
    questionary.print(
        f"Saved {path}. Regenerate with: generate_yaml.py --config {path}\n"
        f"Secrets are not stored: put {GATEWAY_TOKEN_VAR} and the API keys in .env "
        "(or pass --env-file), otherwise a new token is generated on every run.",
        style="fg:ansigreen",
    )


def offer_clipboard_copy(text: str) -> None:
    clipboard = detect_clipboard()
    if clipboard is None:
        questionary.print(
            "No clipboard tool detected (install wl-clipboard, xclip or xsel); "
            "copy the YAML from the file instead.",
            style="fg:ansiyellow",
        )
        return
    if not ask(
        questionary.confirm(
            f"Copy the YAML to your clipboard ({clipboard.name})?", default=True
        )
    ):
        return
    try:
        copy_to_clipboard(text, clipboard)
    except ClipboardError as err:
        questionary.print(
            f"Could not copy: {err}. Copy the YAML from the file instead.",
            style="fg:ansired",
        )
        return
    questionary.print(
        "Copied. The clipboard now holds the token and API keys: clear it after pasting.",
        style="fg:ansigreen",
    )


def report_loaded_secrets(known: dict[str, str], env_file: Path | None) -> None:
    if not known:
        return
    source = f"{env_file} and the environment" if env_file else "the environment"
    questionary.print(
        f"Secrets loaded from {source}: {', '.join(known)}", style="fg:ansicyan"
    )


def read_sources() -> tuple[str, str]:
    return read_source(DOCKERFILE), read_source(ENTRYPOINT)


def run_interactive(output: Path, known: dict[str, str]) -> int:
    dockerfile, entrypoint = read_sources()

    questionary.print("OpenClaw TrueNAS YAML generator", style="bold fg:ansicyan")
    settings = prompt_settings()
    secrets_, token_generated = prompt_secrets(
        known, settings.trusted_proxy is not None
    )

    config = build_app_config(settings, secrets_)
    print_summary(config)
    if not ask(questionary.confirm("Write the YAML?", default=True)):
        return 1

    text = write_yaml(config, output, dockerfile, entrypoint)
    offer_token(config.secrets.gateway_token, token_generated)
    offer_config_save(settings)
    offer_clipboard_copy(text)
    return 0


def secrets_for_config_mode(
    known: dict[str, str], trusted_proxy: bool
) -> tuple[Secrets, bool]:
    """Secrets without prompting; a missing gateway token is generated."""
    warn_ignored_secrets(known, trusted_proxy)
    if trusted_proxy:
        token, generated = "", False
    elif known.get(GATEWAY_TOKEN_VAR):
        token, generated = known[GATEWAY_TOKEN_VAR], False
    else:
        token, generated = secrets.token_hex(32), True
    return (
        Secrets(
            token,
            known.get(ANTHROPIC_KEY_VAR, ""),
            known.get(ANTIGRAVITY_KEY_VAR, ""),
            known.get(GATEWAY_PASSWORD_VAR, "") if trusted_proxy else "",
        ),
        generated,
    )


def run_from_config(config_path: Path, output: Path, known: dict[str, str]) -> int:
    """Non-interactive run: every setting comes from the JSON config."""
    dockerfile, entrypoint = read_sources()
    settings = load_settings(config_path)

    secrets_, token_generated = secrets_for_config_mode(
        known, settings.trusted_proxy is not None
    )
    config = build_app_config(settings, secrets_)
    questionary.print(f"Using config {config_path}", style="bold fg:ansicyan")
    print_summary(config)

    text = write_yaml(config, output, dockerfile, entrypoint)
    if token_generated:
        questionary.print(
            f"{GATEWAY_TOKEN_VAR} not found: generated a random token, stored only in "
            f"the YAML. Add it to your env file to keep the same token on every run.",
            style="fg:ansiyellow",
        )
    if sys.stdin.isatty() and sys.stdout.isatty():
        offer_clipboard_copy(text)
    return 0


def main(argv: list[str] | None = None) -> int:
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
    parser.add_argument(
        "-c",
        "--config",
        type=Path,
        help="JSON config with every setting except secrets; skips all prompts",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        help=(
            "dotenv file with OPENCLAW_GATEWAY_TOKEN, ANTHROPIC_API_KEY and "
            "ANTIGRAVITY_API_KEY (default: ./.env if it exists)"
        ),
    )
    args = parser.parse_args(argv)
    try:
        env_file = resolve_env_file(args.env_file)
        known = load_secret_values(env_file)
        report_loaded_secrets(known, env_file)
        if args.config:
            return run_from_config(args.config, args.output, known)
        return run_interactive(args.output, known)
    except Cancelled:
        print("Cancelled.", file=sys.stderr)
        return 130
    except ConfigError as err:
        print(f"Invalid config:\n{err}", file=sys.stderr)
        return 1
    except (EnvFileError, ValueError) as err:
        print(f"Error: {err}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
