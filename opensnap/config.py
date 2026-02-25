"""Application configuration models and defaults."""

from dataclasses import dataclass
import os

from opensnap.env_loader import load_env_file

DEFAULT_SERVER_HOST = '0.0.0.0'
DEFAULT_SERVER_PORT = 9090
DEFAULT_GAME_PLUGIN = 'automodellista'
DEFAULT_SERVER_SECRET = 'Totally secret server secret!'
DEFAULT_BOOTSTRAP_KEY = 'SNAP-SWAN'
DEFAULT_TICK_INTERVAL_SECONDS = 10.0
DEFAULT_STORAGE_BACKEND = 'sqlite'
DEFAULT_SQLITE_PATH = 'opensnap.db'
DEFAULT_SQLITE_USERS = 'test:1111'
DEFAULT_RESET_RUNTIME_ON_STARTUP = True


@dataclass(frozen=True, slots=True)
class UserConfig:
    """Static user account settings."""

    user_id: int
    username: str
    password: str
    # Empty seed means "generate one secure seed per account".
    seed: str = ''
    team: str = ''


@dataclass(frozen=True, slots=True)
class LobbyConfig:
    """Static lobby settings."""

    lobby_id: int
    name: str


@dataclass(frozen=True, slots=True)
class ServerConfig:
    """Server runtime settings."""

    host: str = DEFAULT_SERVER_HOST
    port: int = DEFAULT_SERVER_PORT
    game_plugin: str = DEFAULT_GAME_PLUGIN
    server_secret: str = DEFAULT_SERVER_SECRET
    bootstrap_key: bytes = DEFAULT_BOOTSTRAP_KEY.encode('utf-8')
    tick_interval_seconds: float = DEFAULT_TICK_INTERVAL_SECONDS


@dataclass(frozen=True, slots=True)
class StorageConfig:
    """Storage backend configuration."""

    backend: str = DEFAULT_STORAGE_BACKEND
    sqlite_path: str = DEFAULT_SQLITE_PATH
    reset_runtime_on_startup: bool = DEFAULT_RESET_RUNTIME_ON_STARTUP


@dataclass(frozen=True, slots=True)
class AppConfig:
    """Top-level application settings."""

    server: ServerConfig
    storage: StorageConfig
    users: tuple[UserConfig, ...]
    lobbies: tuple[LobbyConfig, ...]


def default_app_config() -> AppConfig:
    """Build default settings for local development."""

    load_env_file()
    host = os.getenv('OPENSNAP_HOST', DEFAULT_SERVER_HOST).strip() or DEFAULT_SERVER_HOST
    port = _read_int_env('OPENSNAP_PORT', DEFAULT_SERVER_PORT)
    game_plugin = os.getenv('OPENSNAP_GAME_PLUGIN', DEFAULT_GAME_PLUGIN).strip().lower() or DEFAULT_GAME_PLUGIN
    server_secret = os.getenv('OPENSNAP_SERVER_SECRET', DEFAULT_SERVER_SECRET) or DEFAULT_SERVER_SECRET
    bootstrap_key = (
        os.getenv('OPENSNAP_BOOTSTRAP_KEY', DEFAULT_BOOTSTRAP_KEY).strip() or DEFAULT_BOOTSTRAP_KEY
    ).encode('utf-8')
    tick_interval_seconds = _read_float_env('OPENSNAP_TICK_INTERVAL_SECONDS', DEFAULT_TICK_INTERVAL_SECONDS)
    storage_backend = DEFAULT_STORAGE_BACKEND
    sqlite_path = os.getenv('OPENSNAP_SQLITE_PATH', DEFAULT_SQLITE_PATH).strip() or DEFAULT_SQLITE_PATH
    reset_runtime_on_startup = _read_bool_env(
        'OPENSNAP_RESET_RUNTIME_ON_STARTUP',
        DEFAULT_RESET_RUNTIME_ON_STARTUP,
    )

    return AppConfig(
        server=ServerConfig(
            host=host,
            port=port,
            game_plugin=game_plugin,
            server_secret=server_secret,
            bootstrap_key=bootstrap_key,
            tick_interval_seconds=tick_interval_seconds,
        ),
        storage=StorageConfig(
            backend=storage_backend,
            sqlite_path=sqlite_path,
            reset_runtime_on_startup=reset_runtime_on_startup,
        ),
        users=_read_default_users(),
        # Lobby naming keeps three race groups plus event and club-meeting groups.
        # m-* is mountain, c-* is city, s-* is circuit, plus event and cm.
        lobbies=(
            LobbyConfig(lobby_id=1, name='m-0'),
            LobbyConfig(lobby_id=2, name='m-1'),
            LobbyConfig(lobby_id=3, name='m-2'),
            LobbyConfig(lobby_id=4, name='m-3'),
            LobbyConfig(lobby_id=5, name='m-4'),
            LobbyConfig(lobby_id=6, name='m-5'),
            LobbyConfig(lobby_id=7, name='c-0'),
            LobbyConfig(lobby_id=8, name='c-1'),
            LobbyConfig(lobby_id=9, name='c-2'),
            LobbyConfig(lobby_id=10, name='c-3'),
            LobbyConfig(lobby_id=11, name='c-4'),
            LobbyConfig(lobby_id=12, name='c-5'),
            LobbyConfig(lobby_id=13, name='s-0'),
            LobbyConfig(lobby_id=14, name='s-1'),
            LobbyConfig(lobby_id=15, name='s-2'),
            LobbyConfig(lobby_id=16, name='s-3'),
            LobbyConfig(lobby_id=17, name='s-4'),
            LobbyConfig(lobby_id=18, name='s-5'),
            LobbyConfig(lobby_id=19, name='event'),
            LobbyConfig(lobby_id=20, name='cm'),
        ),
    )


def _read_default_users() -> tuple[UserConfig, ...]:
    """Read default SQLite users from environment override."""

    users_raw = os.getenv('OPENSNAP_DEFAULT_USERS', DEFAULT_SQLITE_USERS).strip()
    users = _parse_default_users(users_raw)
    if users:
        return users
    return _parse_default_users(DEFAULT_SQLITE_USERS)


def _parse_default_users(raw: str) -> tuple[UserConfig, ...]:
    """Parse `username:password[:seed[:team]]` default-user entries."""

    users: list[UserConfig] = []
    for entry in raw.split(','):
        token = entry.strip()
        if not token:
            continue

        parts = token.split(':')
        if len(parts) < 2:
            continue

        username = parts[0].strip()
        password = parts[1].strip()
        if not username or not password:
            continue

        seed = parts[2].strip() if len(parts) >= 3 else ''
        team = ':'.join(parts[3:]).strip() if len(parts) >= 4 else ''
        users.append(
            UserConfig(
                user_id=len(users) + 1,
                username=username,
                password=password,
                seed=seed,
                team=team,
            )
        )

    return tuple(users)


def _read_int_env(key: str, default: int) -> int:
    """Read integer environment value with fallback."""

    raw = os.getenv(key)
    if raw is None:
        return default

    try:
        return int(raw.strip())
    except ValueError:
        return default


def _read_float_env(key: str, default: float) -> float:
    """Read float environment value with fallback."""

    raw = os.getenv(key)
    if raw is None:
        return default

    try:
        return float(raw.strip())
    except ValueError:
        return default


def _read_bool_env(key: str, default: bool) -> bool:
    """Read boolean environment value with fallback."""

    raw = os.getenv(key)
    if raw is None:
        return default

    normalized = raw.strip().lower()
    if normalized in {'1', 'true', 'yes', 'on'}:
        return True
    if normalized in {'0', 'false', 'no', 'off'}:
        return False
    return default
