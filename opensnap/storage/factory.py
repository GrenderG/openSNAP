"""Storage backend factory."""

from typing import Literal

from opensnap.config import AppConfig
from opensnap.storage.interfaces import StorageBundle
from opensnap.storage.sqlite import (
    SqliteAccountDirectory,
    SqliteDatabase,
    SqliteLobbyRegistry,
    SqliteRoomRegistry,
    SqliteSessionRegistry,
)


def create_storage(
    config: AppConfig,
    *,
    reset_mode: Literal['full', 'game', 'none'] = 'full',
) -> StorageBundle:
    """Create storage bundle for configured backend."""

    backend = config.storage.backend
    if backend != 'sqlite':
        raise ValueError(f'Unsupported storage backend: {backend}.')

    database = SqliteDatabase(config.storage.sqlite_path)
    if config.storage.reset_runtime_on_startup and reset_mode == 'full':
        database.reset_runtime_state()
    elif config.storage.reset_runtime_on_startup and reset_mode == 'game':
        database.reset_game_runtime_state()
    database.seed(config.users, config.lobbies)
    return StorageBundle(
        accounts=SqliteAccountDirectory(database),
        sessions=SqliteSessionRegistry(database),
        lobbies=SqliteLobbyRegistry(database),
        rooms=SqliteRoomRegistry(database),
        _close=database.close,
    )
