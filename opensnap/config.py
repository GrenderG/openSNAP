"""Application configuration models and defaults."""

from dataclasses import dataclass
import os


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

    host: str = '0.0.0.0'
    port: int = 9090
    game_plugin: str = 'automodellista'
    server_secret: str = 'Totally secret server secret!'
    bootstrap_key: bytes = b'SNAP-SWAN'
    tick_interval_seconds: float = 10.0


@dataclass(frozen=True, slots=True)
class StorageConfig:
    """Storage backend configuration."""

    backend: str = 'memory'
    sqlite_path: str = 'opensnap.db'


@dataclass(frozen=True, slots=True)
class AppConfig:
    """Top-level application settings."""

    server: ServerConfig
    storage: StorageConfig
    users: tuple[UserConfig, ...]
    lobbies: tuple[LobbyConfig, ...]


def default_app_config() -> AppConfig:
    """Build default settings for local development."""

    game_plugin = os.getenv('OPENSNAP_GAME_PLUGIN', 'automodellista').strip().lower()
    return AppConfig(
        server=ServerConfig(game_plugin=game_plugin),
        storage=StorageConfig(
            backend=os.getenv('OPENSNAP_STORAGE_BACKEND', 'memory').strip().lower(),
            sqlite_path=os.getenv('OPENSNAP_SQLITE_PATH', 'opensnap.db').strip(),
        ),
        users=(
            UserConfig(user_id=1, username='gh0st', password='1111'),
            UserConfig(user_id=2, username='no23', password='1111'),
            UserConfig(user_id=3, username='test', password='1111'),
        ),
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
