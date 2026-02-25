"""Storage backend factory."""

from opensnap.config import AppConfig
from opensnap.storage.interfaces import StorageBundle
from opensnap.storage.sqlite import (
    SqliteAccountDirectory,
    SqliteDatabase,
    SqliteLobbyRegistry,
    SqliteRoomRegistry,
    SqliteSessionRegistry,
)


def create_storage(config: AppConfig) -> StorageBundle:
    """Create storage bundle for configured backend."""

    backend = config.storage.backend
    if backend != 'sqlite':
        raise ValueError(f'Unsupported storage backend: {backend}.')

    database = SqliteDatabase(config.storage.sqlite_path)
    if config.storage.reset_runtime_on_startup:
        database.reset_runtime_state()
    database.seed(config.users, config.lobbies)
    return StorageBundle(
        accounts=SqliteAccountDirectory(database),
        sessions=SqliteSessionRegistry(database),
        lobbies=SqliteLobbyRegistry(database),
        rooms=SqliteRoomRegistry(database),
    )
