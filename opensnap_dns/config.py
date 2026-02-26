"""Configuration for the standalone DNS service."""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
import ipaddress
import json
import os

from opensnap.env_loader import load_env_file

DEFAULT_DNS_HOST = '0.0.0.0'
DEFAULT_DNS_PORT = 53
DEFAULT_DNS_TTL = 60
DEFAULT_DNS_TARGET_IP = '127.0.0.1'
DEFAULT_DNS_ENTRIES: dict[str, str] = {
    'bootstrap.capcom-am.games.sega.net': '@default',
    'gameweb.capcom-am.games.sega.net': '@default',
    'regweb.capcom-am.games.sega.net': '@default',
}


@dataclass(frozen=True, slots=True)
class DnsServerConfig:
    """Runtime settings for the DNS service."""

    host: str = DEFAULT_DNS_HOST
    port: int = DEFAULT_DNS_PORT
    ttl: int = DEFAULT_DNS_TTL
    entries: dict[str, str] = field(default_factory=dict)


def default_dns_server_config() -> DnsServerConfig:
    """Build DNS configuration from environment variables."""

    load_env_file()
    host = os.getenv('OPENSNAP_DNS_HOST', DEFAULT_DNS_HOST).strip() or DEFAULT_DNS_HOST
    port = _read_int_env('OPENSNAP_DNS_PORT', DEFAULT_DNS_PORT, minimum=1)
    ttl = _read_int_env('OPENSNAP_DNS_TTL', DEFAULT_DNS_TTL, minimum=0)

    default_target_ip = _resolve_default_target_ip()
    entries = _resolve_dns_entries(DEFAULT_DNS_ENTRIES, default_target_ip=default_target_ip)
    entries.update(
        _parse_dns_entries(
            os.getenv('OPENSNAP_DNS_ENTRIES', ''),
            default_target_ip=default_target_ip,
        )
    )

    return DnsServerConfig(
        host=host,
        port=port,
        ttl=ttl,
        entries=entries,
    )


def _resolve_default_target_ip() -> str:
    """Resolve fallback IPv4 address used by default DNS entries."""

    candidates = (
        os.getenv('OPENSNAP_DNS_DEFAULT_IP', ''),
        os.getenv('OPENSNAP_ADVERTISE_HOST', ''),
        os.getenv('OPENSNAP_HOST', ''),
    )
    for candidate in candidates:
        parsed = _parse_ipv4(candidate)
        if parsed is None or parsed == '0.0.0.0':
            continue
        return parsed

    return DEFAULT_DNS_TARGET_IP


def _parse_dns_entries(raw_value: str, *, default_target_ip: str) -> dict[str, str]:
    """Parse `OPENSNAP_DNS_ENTRIES` from JSON or Python-literal dict."""

    token = raw_value.strip()
    if not token:
        return {}

    parsed = _parse_json_dict(token)
    if parsed is None:
        parsed = _parse_python_dict(token)
    if parsed is None:
        return {}

    return _resolve_dns_entries(parsed, default_target_ip=default_target_ip)


def _resolve_dns_entries(
    entries_like: dict[object, object],
    *,
    default_target_ip: str,
) -> dict[str, str]:
    """Normalize one DNS dict source into flat `domain -> IPv4` mapping."""

    resolved: dict[str, str] = {}
    for raw_domain, raw_target in entries_like.items():
        if not isinstance(raw_domain, str) or not isinstance(raw_target, str):
            continue

        domain = _normalize_domain(raw_domain)
        target_ip = _resolve_entry_target(raw_target, default_target_ip=default_target_ip)
        if not domain or target_ip is None:
            continue

        resolved[domain] = target_ip

    return resolved


def _resolve_entry_target(raw_target: str, *, default_target_ip: str) -> str | None:
    """Resolve target IP from raw entry value."""

    token = raw_target.strip()
    if not token:
        return None

    if token.lower() in {'@default', 'default', 'auto'}:
        return default_target_ip

    return _parse_ipv4(token)


def _parse_json_dict(raw_value: str) -> dict[object, object] | None:
    """Parse JSON object safely."""

    try:
        parsed = json.loads(raw_value)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    return parsed


def _parse_python_dict(raw_value: str) -> dict[object, object] | None:
    """Parse Python-literal dict safely."""

    try:
        parsed = ast.literal_eval(raw_value)
    except (SyntaxError, ValueError):
        return None
    if not isinstance(parsed, dict):
        return None
    return parsed


def _normalize_domain(domain: str) -> str:
    """Normalize FQDN lookup key."""

    return domain.strip().rstrip('.').lower()


def _parse_ipv4(value: str) -> str | None:
    """Validate and normalize IPv4 text."""

    token = value.strip()
    if not token:
        return None

    try:
        address = ipaddress.ip_address(token)
    except ValueError:
        return None

    if address.version != 4:
        return None
    return str(address)


def _read_int_env(key: str, default: int, *, minimum: int) -> int:
    """Read integer environment variable with fallback bounds."""

    raw_value = os.getenv(key)
    if raw_value is None:
        return default

    try:
        parsed = int(raw_value.strip())
    except ValueError:
        return default

    if parsed < minimum:
        return default
    return parsed
