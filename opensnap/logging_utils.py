"""Logging setup and packet diagnostic helpers."""

from collections.abc import Iterable
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
from typing import NoReturn

DEFAULT_LOG_LEVEL = 'debug'
DEFAULT_HEXDUMP_LIMIT = 16384
DEFAULT_LOG_PATH = ''
DEFAULT_LOG_MAX_MB = 50
DEFAULT_LOG_BACKUP_COUNT = 5
SERVICE_LOG_FILENAMES = {
    'bootstrap': 'opensnap-bootstrap.log',
    'game': 'opensnap-game.log',
    'dns': 'opensnap-dns.log',
    'web': 'opensnap-web.log',
    'capcom-app': 'opensnap-capcom-app.log',
}
LOG_LEVELS: dict[str, int] = {
    'critical': logging.CRITICAL,
    'error': logging.ERROR,
    'warn': logging.WARNING,
    'warning': logging.WARNING,
    'info': logging.INFO,
    'debug': logging.DEBUG,
}


def configure_logging(
    level_name: str | None = None,
    *,
    service_name: str,
    instance_name: str | None = None,
) -> None:
    """Configure process logging for openSNAP services.

    `instance_name` gives each process of one service its own log file
    (`opensnap-game-<instance>.log`), so concurrent processes never rotate
    the same file.
    """

    configured_level = parse_log_level(level_name or os.getenv('OPENSNAP_LOG_LEVEL', DEFAULT_LOG_LEVEL))
    configured_log_path = os.getenv('OPENSNAP_LOG_PATH', DEFAULT_LOG_PATH).strip()
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    resolved_log_file_path: str | None = None
    file_handler = _build_file_handler(configured_log_path, service_name, instance_name)
    if file_handler is not None:
        handlers.append(file_handler[0])
        resolved_log_file_path = file_handler[1]

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
    if configured_log_path:
        if resolved_log_file_path is None:
            logger.warning(
                (
                    'Failed to enable OPENSNAP_LOG_PATH=%s for %s; '
                    'continuing with console logging only.'
                ),
                configured_log_path,
                service_name,
            )
        else:
            logger.info('File logging enabled at %s.', resolved_log_file_path)


def exit_with_logged_os_error(
    logger: logging.Logger,
    *,
    service_name: str,
    error: OSError,
) -> NoReturn:
    """Log one fatal service `OSError` with traceback, then exit non-zero."""

    logger.exception('Fatal %s service OSError: %s', service_name, error)
    raise SystemExit(1) from error


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


def parse_log_max_mb(value: str | None) -> int:
    """Parse the per-file log size cap in MiB; 0 disables rotation."""

    try:
        max_mb = int((value or '').strip())
    except ValueError:
        return DEFAULT_LOG_MAX_MB
    return max_mb if max_mb >= 0 else DEFAULT_LOG_MAX_MB


def parse_log_backup_count(value: str | None) -> int:
    """Parse how many rotated log files to keep (at least one)."""

    try:
        count = int((value or '').strip())
    except ValueError:
        return DEFAULT_LOG_BACKUP_COUNT
    return count if count >= 1 else DEFAULT_LOG_BACKUP_COUNT


def _build_file_handler(
    log_path: str,
    service_name: str,
    instance_name: str | None,
) -> tuple[logging.FileHandler, str] | None:
    """Build optional size-rotated file handler for runtime logging."""

    if not log_path:
        return None

    try:
        filename = SERVICE_LOG_FILENAMES[service_name]
        if instance_name:
            filename = filename.replace('.log', f'-{instance_name}.log')
        path = Path(log_path).expanduser()
        if not path.is_absolute():
            path = (Path.cwd() / path).resolve()
        path.mkdir(parents=True, exist_ok=True)
        file_path = path / filename
        handler = RotatingFileHandler(
            file_path,
            maxBytes=parse_log_max_mb(os.getenv('OPENSNAP_LOG_MAX_MB')) * 1024 * 1024,
            backupCount=parse_log_backup_count(os.getenv('OPENSNAP_LOG_BACKUP_COUNT')),
            encoding='utf-8',
        )
    except (KeyError, OSError):
        return None

    return handler, str(file_path)


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
