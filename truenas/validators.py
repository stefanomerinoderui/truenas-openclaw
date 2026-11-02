"""Input validators shared by the interactive prompts and the JSON config loader.

Every ``validate_*`` function returns ``True`` or an error message, which is the
contract questionary expects from its ``validate`` callbacks.
"""

from __future__ import annotations

import ipaddress
import re
from urllib.parse import urlparse

from compose_builder import DEFAULT_IMAGE, is_ipv6

NETWORK_NAME_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
HOSTNAME_PATTERN = re.compile(r"[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?")
HEADER_NAME_PATTERN = re.compile(r"[A-Za-z0-9_-]+")


def split_list(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def parse_bind_addresses(value: str) -> tuple[str, ...]:
    """Normalise and de-duplicate a validated comma-separated address list."""
    addresses = (
        str(ipaddress.ip_address(item.strip("[]"))) for item in split_list(value)
    )
    return tuple(dict.fromkeys(addresses))


def validate_port(value: str) -> bool | str:
    if value.isdigit() and 1 <= int(value) <= 65535:
        return True
    return "Enter a port between 1 and 65535"


def validate_bind_addresses(value: str) -> bool | str:
    addresses = []
    for item in split_list(value):
        try:
            addresses.append(ipaddress.ip_address(item.strip("[]")))
        except ValueError:
            return f"Not a valid IPv4/IPv6 address: {item}"
    if not addresses:
        return "Enter at least one address (0.0.0.0 = all interfaces)"
    for version in (4, 6):
        same_family = {ip for ip in addresses if ip.version == version}
        if len(same_family) > 1 and any(ip.is_unspecified for ip in same_family):
            return (
                f"The IPv{version} wildcard address already covers every other "
                f"IPv{version} address; use it alone or list specific addresses"
            )
    return True


def validate_network_name(value: str) -> bool | str:
    if NETWORK_NAME_PATTERN.fullmatch(value.strip()):
        return True
    return "Use letters, digits, '_', '.' or '-' (e.g. ix-nginx-proxy-manager_default)"


def validate_hostname(value: str) -> bool | str:
    if HOSTNAME_PATTERN.fullmatch(value.strip()):
        return True
    return "Use letters, digits, '-' or '.' (must start and end with a letter/digit)"


def validate_trusted_proxies(value: str) -> bool | str:
    """Proxy IPs or CIDRs, e.g. the proxy network's subnet."""
    items = split_list(value)
    if not items:
        return "Enter at least one proxy IP or CIDR (e.g. 172.18.0.0/16)"
    for item in items:
        try:
            network = ipaddress.ip_network(item, strict=False)
        except ValueError:
            return f"Not a valid IP address or CIDR: {item}"
        if network.prefixlen == 0:
            return f"{item} would trust every source; list only proxies you control"
    return True


def validate_header_name(value: str) -> bool | str:
    if HEADER_NAME_PATTERN.fullmatch(value.strip()):
        return True
    return "Enter an HTTP header name, e.g. x-forwarded-user"


def validate_header_names(value: str) -> bool | str:
    """Optional comma-separated header names."""
    for item in split_list(value):
        if validate_header_name(item) is not True:
            return f"Not an HTTP header name: {item}"
    return True


def validate_identities(value: str) -> bool | str:
    """Optional comma-separated user identities (emails, usernames)."""
    for item in split_list(value):
        if re.search(r"\s", item):
            return f"Identities can't contain spaces: {item}"
    return True


def validate_hosts(value: str) -> bool | str:
    hosts = split_list(value)
    if not hosts:
        return "Enter at least one hostname or IP"
    for host in hosts:
        if "/" in host or (":" in host and not is_ipv6(host)):
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


def validate_required_urls(value: str) -> bool | str:
    if not split_list(value):
        return "Enter at least one public URL, e.g. https://openclaw.example.com"
    return validate_urls(value)


def validate_dataset_path(value: str) -> bool | str:
    if value.startswith("/mnt/") and len(value.rstrip("/")) > len("/mnt"):
        return True
    return "TrueNAS datasets live under /mnt/<pool>/..."


def validate_required(value: str) -> bool | str:
    return True if value.strip() else "This value is required"


def validate_image(value: str) -> bool | str:
    if value.strip() and " " not in value.strip():
        return True
    return f"Enter an image reference, e.g. {DEFAULT_IMAGE}"
