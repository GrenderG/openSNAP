"""Logging setup and packet diagnostic helpers."""

from collections.abc import Iterable
import logging
import os
from pathlib import Path

DEFAULT_LOG_LEVEL = 'debug'
DEFAULT_HEXDUMP_LIMIT = 16384
DEFAULT_LOG_FILE = ''
LOG_LEVELS: dict[str, int] = {
    'critical': logging.CRITICAL,
    'error': logging.ERROR,
    'warn': logging.WARNING,
    'warning': logging.WARNING,
    'info': logging.INFO,
    'debug': logging.DEBUG,
}


def configure_logging(level_name: str | None = None, *, log_file: str | None = None) -> None:
    """Configure process logging for openSNAP services."""

    configured_level = parse_log_level(level_name or os.getenv('OPENSNAP_LOG_LEVEL', DEFAULT_LOG_LEVEL))
    configured_log_file = (log_file if log_file is not None else os.getenv('OPENSNAP_LOG_FILE', DEFAULT_LOG_FILE)).strip()
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    log_file_path: str | None = None
    file_handler = _build_file_handler(configured_log_file)
    if file_handler is not None:
        handlers.append(file_handler[0])
        log_file_path = file_handler[1]

    logging.basicConfig(
        level=configured_level,
        format='%(asctime)s %(levelname)s [%(name)s] %(message)s',
        handlers=handlers,
        force=True,
    )
    logger = logging.getLogger('opensnap')
    logger.info(
        'Logging initialized at level %s.',
        logging.getLevelName(configured_level).lower(),
    )
    if configured_log_file:
        if log_file_path is None:
            logger.warning('Failed to enable OPENSNAP_LOG_FILE=%s; continuing with console logging only.', configured_log_file)
        else:
            logger.info('File logging enabled at %s.', log_file_path)


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


def _build_file_handler(log_file: str) -> tuple[logging.FileHandler, str] | None:
    """Build optional file handler for runtime logging."""

    if not log_file:
        return None

    try:
        path = Path(log_file).expanduser()
        if not path.is_absolute():
            path = (Path.cwd() / path).resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(path, encoding='utf-8')
    except OSError:
        return None

    return handler, str(path)


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
