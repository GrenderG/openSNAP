"""Simple `.env` loader for local configuration overrides."""

from pathlib import Path
import os
import shutil

_loaded_paths: set[Path] = set()
_REPO_ROOT = Path(__file__).resolve().parents[1]


def load_env_file(path: str | None = None) -> None:
    """Load environment config with `.env` then `.env.dist` fallback."""

    if path is not None:
        _load_single_file(path)
        return

    explicit_path = os.getenv('OPENSNAP_ENV_FILE', '').strip()
    dist_name = os.getenv('OPENSNAP_ENV_DIST_FILE', '.env.dist').strip() or '.env.dist'
    base_dir = _default_base_dir(dist_name)
    dist_path = base_dir / dist_name
    if explicit_path:
        _load_single_file(explicit_path)
    else:
        env_path = base_dir / '.env'
        _bootstrap_env_from_dist(env_path=env_path, dist_path=dist_path)
        _load_single_file(env_path)

    # `.env.dist` backfills any missing keys not set by shell or `.env`.
    _load_single_file(dist_path)


def _default_base_dir(dist_name: str) -> Path:
    """Resolve base directory for default `.env` lookup."""

    cwd = Path.cwd()
    if (cwd / '.env').exists() or (cwd / dist_name).exists():
        return cwd
    return _REPO_ROOT


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


def _load_single_file(path: str | Path) -> None:
    """Load one env file without overriding existing vars."""

    env_path = Path(path).expanduser()
    if not env_path.is_absolute():
        env_path = (Path.cwd() / env_path).resolve()
    else:
        env_path = env_path.resolve()
    if env_path in _loaded_paths:
        return

    _loaded_paths.add(env_path)
    if not env_path.exists():
        return

    lines = env_path.read_text(encoding='utf-8').splitlines()
    line_index = 0
    while line_index < len(lines):
        raw_line = lines[line_index]
        line = raw_line.strip()
        if not line or line.startswith('#'):
            line_index += 1
            continue

        if line.startswith('export '):
            line = line[7:].strip()
        if '=' not in line:
            line_index += 1
            continue

        key, value = line.split('=', 1)
        key = key.strip()
        value = value.strip()
        if value.startswith('{'):
            value, line_index = _collect_multiline_dict_value(
                lines,
                start_index=line_index,
                initial_value=value,
            )
        value = _strip_quotes(value.strip())
        if not key or key in os.environ:
            line_index += 1
            continue
        os.environ[key] = value
        line_index += 1


def _collect_multiline_dict_value(
    lines: list[str],
    *,
    start_index: int,
    initial_value: str,
) -> tuple[str, int]:
    """Collect a `{...}` env value that spans multiple lines."""

    value_lines = [initial_value]
    balance = _brace_balance(initial_value)
    index = start_index
    while balance > 0 and (index + 1) < len(lines):
        index += 1
        raw_line = lines[index]
        stripped = raw_line.strip()
        if stripped.startswith('#'):
            continue
        value_lines.append(raw_line.rstrip())
        balance += _brace_balance(raw_line)

    return '\n'.join(value_lines), index


def _brace_balance(value: str) -> int:
    """Return unmatched-brace balance for simple `{...}` accumulation."""

    return value.count('{') - value.count('}')


def _strip_quotes(value: str) -> str:
    """Strip matching single or double quotes around a value."""

    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    return value
