"""Storage backend factory."""

from opensnap.config import AppConfig
from opensnap.core.accounts import AccountDirectory
from opensnap.core.lobbies import LobbyRegistry
from opensnap.core.rooms import RoomRegistry
from opensnap.core.sessions import SessionRegistry
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
    if backend == 'memory':
        return StorageBundle(
            accounts=AccountDirectory(config.users),
            sessions=SessionRegistry(),
            lobbies=LobbyRegistry(config.lobbies),
            rooms=RoomRegistry(),
        )

    if backend == 'sqlite':
        database = SqliteDatabase(config.storage.sqlite_path)
        database.seed(config.users, config.lobbies)
        return StorageBundle(
            accounts=SqliteAccountDirectory(database),
            sessions=SqliteSessionRegistry(database),
            lobbies=SqliteLobbyRegistry(database),
            rooms=SqliteRoomRegistry(database),
        )

    raise ValueError(f'Unsupported storage backend: {backend}.')
