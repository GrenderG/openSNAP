"""Shared storage backend factory."""

from collections.abc import Callable

from opensnap.config import AppConfig, StorageConfig
from opensnap.storage.interfaces import StorageBundle
from opensnap.storage.mariadb import MariaDbConnection
from opensnap.storage.postgresql import PostgresqlConnection
from opensnap.storage.sql import (
    SqlAccountStore,
    SqlConnection,
    SqlOnlinePlayerStore,
    SqlRecordStore,
    SqlSessionHandoffStore,
)
from opensnap.storage.sqlite import SqliteConnection

# Backend name -> connection opener. Each backend module imports its database
# driver only when it connects, so the optional drivers stay optional.
BACKENDS: dict[str, Callable[[StorageConfig], SqlConnection]] = {
    'sqlite': lambda storage: SqliteConnection(storage.sqlite_path),
    'mariadb': MariaDbConnection,
    'postgresql': PostgresqlConnection,
}
SUPPORTED_BACKENDS = tuple(BACKENDS)


def create_storage(config: AppConfig) -> StorageBundle:
    """Open the configured shared store (accounts, session handoffs, records, online players)."""

    connection = open_connection(config)
    return StorageBundle(
        accounts=SqlAccountStore(connection, server_secret=config.server.server_secret),
        handoffs=SqlSessionHandoffStore(connection),
        records=SqlRecordStore(connection),
        online_players=SqlOnlinePlayerStore(connection),
        _close=connection.close,
    )


def open_connection(config: AppConfig) -> SqlConnection:
    """Connect to the configured backend."""

    opener = BACKENDS.get(config.storage.backend)
    if opener is None:
        raise ValueError(
            f'Unsupported storage backend: {config.storage.backend}. Supported: {", ".join(SUPPORTED_BACKENDS)}.'
        )
    return opener(config.storage)
