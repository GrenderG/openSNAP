"""Application configuration models and defaults."""

import ast
from dataclasses import dataclass, field
import json
import os

from opensnap.env_loader import load_env_file

DEFAULT_BOOTSTRAP_HOST = '0.0.0.0'
DEFAULT_BOOTSTRAP_ADVERTISE_HOST = ''
DEFAULT_BOOTSTRAP_PORT = 9090
DEFAULT_GAME_HOST = '0.0.0.0'
DEFAULT_GAME_ADVERTISE_HOST = ''
DEFAULT_GAME_PORT = 9091
DEFAULT_GAME_IDENTIFIER = 'automodellista'
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
class ServiceEndpointConfig:
    """Bind and advertised endpoint settings for one UDP service."""

    host: str
    # Advertised host encoded into protocol payloads.
    # Empty means "derive from bind host and routing".
    advertise_host: str
    port: int


@dataclass(frozen=True, slots=True)
class GameServerTargetConfig:
    """Configured bootstrap target for one game identifier."""

    game_identifier: str
    host: str
    port: int


def _default_bootstrap_endpoint() -> ServiceEndpointConfig:
    """Build the default bootstrap endpoint config."""

    return ServiceEndpointConfig(
        host=DEFAULT_BOOTSTRAP_HOST,
        advertise_host=DEFAULT_BOOTSTRAP_ADVERTISE_HOST,
        port=DEFAULT_BOOTSTRAP_PORT,
    )


def _default_game_endpoint() -> ServiceEndpointConfig:
    """Build the default game endpoint config."""

    return ServiceEndpointConfig(
        host=DEFAULT_GAME_HOST,
        advertise_host=DEFAULT_GAME_ADVERTISE_HOST,
        port=DEFAULT_GAME_PORT,
    )


@dataclass(frozen=True, slots=True)
class ServerConfig:
    """Server runtime settings."""

    bootstrap: ServiceEndpointConfig = field(default_factory=_default_bootstrap_endpoint)
    game: ServiceEndpointConfig = field(default_factory=_default_game_endpoint)
    game_identifier: str = DEFAULT_GAME_IDENTIFIER
    game_plugin: str = DEFAULT_GAME_PLUGIN
    default_bootstrap_game_identifier: str = DEFAULT_GAME_IDENTIFIER
    game_targets: tuple[GameServerTargetConfig, ...] = ()
    server_secret: str = DEFAULT_SERVER_SECRET
    bootstrap_key: bytes = DEFAULT_BOOTSTRAP_KEY.encode('utf-8')
    tick_interval_seconds: float = DEFAULT_TICK_INTERVAL_SECONDS

    @property
    def host(self) -> str:
        """Compatibility alias for the game bind host."""

        return self.game.host

    @property
    def advertise_host(self) -> str:
        """Compatibility alias for the game advertised host."""

        return self.game.advertise_host

    @property
    def port(self) -> int:
        """Compatibility alias for the game port."""

        return self.game.port

    def resolve_game_target(self, game_identifier: str) -> GameServerTargetConfig | None:
        """Resolve bootstrap target settings for one game identifier."""

        for target in self.game_targets:
            if target.game_identifier == game_identifier:
                return target
        return None


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
    bootstrap_host = _read_text_env(
        ('OPENSNAP_BOOTSTRAP_HOST',),
        DEFAULT_BOOTSTRAP_HOST,
    )
    bootstrap_advertise_host = _read_text_env(
        ('OPENSNAP_BOOTSTRAP_ADVERTISE_HOST',),
        DEFAULT_BOOTSTRAP_ADVERTISE_HOST,
        allow_empty=True,
    )
    bootstrap_port = _read_int_env(
        ('OPENSNAP_BOOTSTRAP_PORT',),
        DEFAULT_BOOTSTRAP_PORT,
    )
    game_host = _read_text_env(
        ('OPENSNAP_GAME_HOST', 'OPENSNAP_HOST'),
        DEFAULT_GAME_HOST,
    )
    game_advertise_host = _read_text_env(
        ('OPENSNAP_GAME_ADVERTISE_HOST', 'OPENSNAP_ADVERTISE_HOST'),
        DEFAULT_GAME_ADVERTISE_HOST,
        allow_empty=True,
    )
    game_port = _read_int_env(
        ('OPENSNAP_GAME_PORT', 'OPENSNAP_PORT'),
        DEFAULT_GAME_PORT,
    )
    game_plugin = os.getenv('OPENSNAP_GAME_PLUGIN', DEFAULT_GAME_PLUGIN).strip().lower() or DEFAULT_GAME_PLUGIN
    game_identifier = _read_text_env(
        ('OPENSNAP_GAME_IDENTIFIER',),
        game_plugin or DEFAULT_GAME_IDENTIFIER,
    ).lower()
    default_bootstrap_game_identifier = _read_text_env(
        ('OPENSNAP_BOOTSTRAP_DEFAULT_GAME_IDENTIFIER',),
        game_identifier or game_plugin,
    ).lower()
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
    game_targets = _read_game_targets_from_env(
        os.getenv('OPENSNAP_GAME_SERVER_MAP', ''),
        current_game_identifier=game_identifier or game_plugin,
        current_game_advertise_host=game_advertise_host,
        current_game_bind_host=game_host,
        current_game_port=game_port,
    )

    return AppConfig(
        server=ServerConfig(
            bootstrap=ServiceEndpointConfig(
                host=bootstrap_host,
                advertise_host=bootstrap_advertise_host,
                port=bootstrap_port,
            ),
            game=ServiceEndpointConfig(
                host=game_host,
                advertise_host=game_advertise_host,
                port=game_port,
            ),
            game_identifier=game_identifier or game_plugin,
            game_plugin=game_plugin,
            default_bootstrap_game_identifier=default_bootstrap_game_identifier,
            game_targets=game_targets,
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
        # Lobby naming keeps three game groups plus event and club-meeting groups.
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


def _read_text_env(keys: tuple[str, ...], default: str, *, allow_empty: bool = False) -> str:
    """Read the first non-empty text environment value with fallback."""

    for key in keys:
        raw = os.getenv(key)
        if raw is None:
            continue

        value = raw.strip()
        if value or allow_empty:
            return value

    return default


def _read_game_targets_from_env(
    raw_value: str,
    *,
    current_game_identifier: str,
    current_game_advertise_host: str,
    current_game_bind_host: str,
    current_game_port: int,
) -> tuple[GameServerTargetConfig, ...]:
    """Build explicit bootstrap game targets from config plus env overrides."""

    targets: list[GameServerTargetConfig] = []
    parsed = _parse_game_target_map(raw_value)
    for identifier, host, port in parsed:
        if identifier not in {target.game_identifier for target in targets}:
            targets.append(
                GameServerTargetConfig(
                    game_identifier=identifier,
                    host=host,
                    port=port,
                )
            )

    if not any(target.game_identifier == current_game_identifier for target in targets):
        targets.append(
            GameServerTargetConfig(
                game_identifier=current_game_identifier,
                host=current_game_advertise_host or current_game_bind_host,
                port=current_game_port,
            )
        )

    return tuple(targets)


def _parse_game_target_map(raw_value: str) -> tuple[tuple[str, str, int], ...]:
    """Parse `OPENSNAP_GAME_SERVER_MAP` from a `game_identifier -> host:port` dict."""

    token = raw_value.strip()
    if not token:
        return ()

    parsed = _parse_json_dict(token)
    if parsed is None:
        parsed = _parse_python_dict(token)
    if parsed is None:
        return ()

    targets: list[tuple[str, str, int]] = []
    for raw_identifier, raw_target in parsed.items():
        if not isinstance(raw_identifier, str):
            continue

        identifier = raw_identifier.strip().lower()
        if not identifier:
            continue

        resolved = _parse_game_target_value(raw_target)
        if resolved is None:
            continue

        targets.append((identifier, resolved[0], resolved[1]))

    return tuple(targets)


def _parse_game_target_value(value: object) -> tuple[str, int] | None:
    """Parse one `game_identifier -> host:port` target value."""

    if isinstance(value, str):
        token = value.strip()
        if not token:
            return None

        host, separator, port_text = token.rpartition(':')
        if not separator:
            return None

        host = host.strip()
        if not host:
            return None

        try:
            port = int(port_text.strip())
        except ValueError:
            return None
        if port <= 0:
            return None
        return host, port

    if isinstance(value, dict):
        raw_host = value.get('host')
        raw_port = value.get('port')
        if not isinstance(raw_host, str):
            return None

        host = raw_host.strip()
        if not host:
            return None

        if isinstance(raw_port, str):
            try:
                port = int(raw_port.strip())
            except ValueError:
                return None
        elif isinstance(raw_port, int):
            port = raw_port
        else:
            return None

        if port <= 0:
            return None
        return host, port

    return None


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


def _read_int_env(keys: tuple[str, ...], default: int) -> int:
    """Read integer environment value with fallback."""

    for key in keys:
        raw = os.getenv(key)
        if raw is None:
            continue

        try:
            return int(raw.strip())
        except ValueError:
            continue

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
