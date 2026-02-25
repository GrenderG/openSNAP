"""Simple `.env` loader for local configuration overrides."""

from pathlib import Path
import os
import shutil

_loaded_paths: set[Path] = set()


def load_env_file(path: str | None = None) -> None:
    """Load environment config with `.env` then `.env.dist` fallback."""

    if path is not None:
        _load_single_file(path)
        return

    explicit_path = os.getenv('OPENSNAP_ENV_FILE', '').strip()
    dist_path = os.getenv('OPENSNAP_ENV_DIST_FILE', '.env.dist')
    if explicit_path:
        _load_single_file(explicit_path)
    else:
        _bootstrap_env_from_dist(env_path=Path('.env'), dist_path=Path(dist_path))
        _load_single_file('.env')

    # `.env.dist` backfills any missing keys not set by shell or `.env`.
    _load_single_file(dist_path)


def _bootstrap_env_from_dist(*, env_path: Path, dist_path: Path) -> None:
    """Create `.env` from `.env.dist` on first run when missing."""

    resolved_env = env_path.resolve()
    resolved_dist = dist_path.resolve()
    if resolved_env.exists() or not resolved_dist.exists():
        return

    try:
        shutil.copyfile(resolved_dist, resolved_env)
    except OSError:
        return


def _load_single_file(path: str) -> None:
    """Load one env file without overriding existing vars."""

    env_path = Path(path).resolve()
    if env_path in _loaded_paths:
        return

    _loaded_paths.add(env_path)
    if not env_path.exists():
        return

    for raw_line in env_path.read_text(encoding='utf-8').splitlines():
        line = raw_line.strip()
        if not line or line.startswith('#'):
            continue

        if line.startswith('export '):
            line = line[7:].strip()
        if '=' not in line:
            continue

        key, value = line.split('=', 1)
        key = key.strip()
        value = _strip_quotes(value.strip())
        if not key or key in os.environ:
            continue
        os.environ[key] = value


def _strip_quotes(value: str) -> str:
    """Strip matching single or double quotes around a value."""

    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    return value
