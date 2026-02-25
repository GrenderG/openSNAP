"""Logging setup and packet diagnostic helpers."""

from collections.abc import Iterable
import logging
import os

DEFAULT_LOG_LEVEL = 'debug'
DEFAULT_HEXDUMP_LIMIT = 16384
LOG_LEVELS: dict[str, int] = {
    'critical': logging.CRITICAL,
    'error': logging.ERROR,
    'warn': logging.WARNING,
    'warning': logging.WARNING,
    'info': logging.INFO,
    'debug': logging.DEBUG,
}


def configure_logging(level_name: str | None = None) -> None:
    """Configure process logging for openSNAP services."""

    configured_level = parse_log_level(level_name or os.getenv('OPENSNAP_LOG_LEVEL', DEFAULT_LOG_LEVEL))
    logging.basicConfig(
        level=configured_level,
        format='%(asctime)s %(levelname)s [%(name)s] %(message)s',
        force=True,
    )
    logging.getLogger('opensnap').info(
        'Logging initialized at level %s.',
        logging.getLevelName(configured_level).lower(),
    )


def parse_log_level(level_name: str) -> int:
    """Parse textual log level with safe fallback to INFO."""

    normalized = level_name.strip().lower()
    return LOG_LEVELS.get(normalized, logging.INFO)


def parse_hexdump_limit(limit_value: str | None) -> int:
    """Parse max hexdump bytes with safe fallback."""

    if limit_value is None:
        return DEFAULT_HEXDUMP_LIMIT

    try:
        limit = int(limit_value.strip())
    except ValueError:
        return DEFAULT_HEXDUMP_LIMIT

    if limit == 0:
        return 0
    if limit < 0:
        return DEFAULT_HEXDUMP_LIMIT
    return limit


def format_hexdump(data: bytes, *, width: int = 16, max_bytes: int | None = None) -> str:
    """Render deterministic hexdump text with offsets and printable ASCII."""

    if not data:
        return '<empty>'

    limit = max_bytes
    if limit is None:
        limit = parse_hexdump_limit(os.getenv('OPENSNAP_LOG_HEXDUMP_LIMIT'))

    view = data if limit == 0 else data[:limit]
    lines = list(_iter_hexdump_lines(view, width=width))
    if len(data) > len(view):
        lines.append(
            f'... truncated {len(data) - len(view)} byte(s); '
            f'raise OPENSNAP_LOG_HEXDUMP_LIMIT or set it to 0 for unlimited output.'
        )
    return '\n'.join(lines)


def _iter_hexdump_lines(data: bytes, *, width: int) -> Iterable[str]:
    """Yield formatted hexdump lines for a byte buffer."""

    for offset in range(0, len(data), width):
        chunk = data[offset:offset + width]
        hex_bytes = ' '.join(f'{byte_value:02x}' for byte_value in chunk).ljust((width * 3) - 1)
        ascii_bytes = ''.join(chr(byte_value) if 32 <= byte_value <= 126 else '.' for byte_value in chunk)
        yield f'{offset:04x}  {hex_bytes}  {ascii_bytes}'
