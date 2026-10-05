"""Shared storage backend factory."""

from opensnap.config import AppConfig
from opensnap.storage.interfaces import StorageBundle
from opensnap.storage.mariadb import MariaDbConnection
from opensnap.storage.sql import SqlAccountStore, SqlConnection, SqlRecordStore, SqlSessionHandoffStore, seed_users
from opensnap.storage.sqlite import SqliteConnection

SUPPORTED_BACKENDS = ('sqlite', 'mariadb')


def create_storage(config: AppConfig) -> StorageBundle:
    """Open the configured shared store (accounts, session handoffs, records)."""

    connection = open_connection(config)
    seed_users(connection, config.users)
    return StorageBundle(
        accounts=SqlAccountStore(connection),
        handoffs=SqlSessionHandoffStore(connection),
        records=SqlRecordStore(connection),
        _close=connection.close,
    )


def open_connection(config: AppConfig) -> SqlConnection:
    """Connect to the configured backend."""

    backend = config.storage.backend
    if backend == 'sqlite':
        return SqliteConnection(config.storage.sqlite_path)
    if backend == 'mariadb':
        return MariaDbConnection(config.storage)
    raise ValueError(f'Unsupported storage backend: {backend}. Supported: {", ".join(SUPPORTED_BACKENDS)}.')
