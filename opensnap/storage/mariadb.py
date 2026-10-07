"""MariaDB/MySQL shared-store backend, for servers spread over several machines.

Requires the optional `PyMySQL` package (`pip install PyMySQL`).
"""

import threading

from opensnap.config import StorageConfig
from opensnap.storage.sql import Row


class MariaDbConnection:
    """Thread-safe MariaDB connection implementing `SqlConnection`."""

    def __init__(self, config: StorageConfig) -> None:
        try:
            import pymysql
            import pymysql.cursors
        except ImportError as exc:
            raise RuntimeError(
                'OPENSNAP_STORAGE_BACKEND=mariadb needs the PyMySQL package (pip install PyMySQL).'
            ) from exc
        if not config.mariadb_host or not config.mariadb_user:
            raise ValueError('OPENSNAP_MARIADB_HOST and OPENSNAP_MARIADB_USER are required for MariaDB storage.')

        self.integrity_error = pymysql.err.IntegrityError
        self._pymysql = pymysql
        self._connect_arguments = {
            'host': config.mariadb_host,
            'port': config.mariadb_port,
            'user': config.mariadb_user,
            'password': config.mariadb_password,
            'database': config.mariadb_database,
            'ssl': {'ca': config.mariadb_ssl_ca} if config.mariadb_ssl_ca else None,
            'charset': 'utf8mb4',
            'autocommit': True,
            'cursorclass': pymysql.cursors.DictCursor,
        }
        self._lock = threading.Lock()
        self._connection = pymysql.connect(**self._connect_arguments)
        self._setup_schema()

    def execute(self, query: str, parameters: tuple[object, ...] = ()) -> None:
        self._write(query, parameters)

    def insert(self, query: str, parameters: tuple[object, ...] = (), *, key: str) -> int:
        # `key` is the table's AUTO_INCREMENT column, which PyMySQL reports as lastrowid.
        del key
        return self._write(query, parameters)

    def insert_or_ignore(self, query: str, parameters: tuple[object, ...] = ()) -> None:
        self._write(query.replace('INSERT INTO', 'INSERT IGNORE INTO', 1), parameters)

    def query_one(self, query: str, parameters: tuple[object, ...] = ()) -> Row | None:
        with self._lock, self._cursor() as cursor:
            cursor.execute(_placeholders(query), parameters)
            return cursor.fetchone()

    def query_all(self, query: str, parameters: tuple[object, ...] = ()) -> list[Row]:
        with self._lock, self._cursor() as cursor:
            cursor.execute(_placeholders(query), parameters)
            return list(cursor.fetchall())

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def _write(self, query: str, parameters: tuple[object, ...]) -> int:
        with self._lock, self._cursor() as cursor:
            cursor.execute(_placeholders(query), parameters)
            return int(cursor.lastrowid or 0)

    def _cursor(self):
        # Servers stay idle for long periods; reconnect if the server dropped us.
        try:
            self._connection.ping()
        except self._pymysql.err.Error:
            self._connection = self._pymysql.connect(**self._connect_arguments)
        return self._connection.cursor()

    def _setup_schema(self) -> None:
        self.execute(
            'CREATE TABLE IF NOT EXISTS users ('
            'user_id INT AUTO_INCREMENT PRIMARY KEY, '
            'username VARCHAR(255) NOT NULL UNIQUE, '
            'password TEXT NOT NULL, '
            'seed TEXT NOT NULL, '
            "team VARCHAR(255) NOT NULL DEFAULT ''"
            ') ENGINE=InnoDB DEFAULT CHARSET=utf8mb4'
        )
        self.execute(
            'CREATE TABLE IF NOT EXISTS session_handoffs ('
            'serial BIGINT AUTO_INCREMENT PRIMARY KEY, '
            'session_id BIGINT UNSIGNED NOT NULL UNIQUE, '
            'user_id INT NOT NULL, '
            'username VARCHAR(255) NOT NULL, '
            'host VARCHAR(255) NOT NULL, '
            'port INT NOT NULL, '
            'game VARCHAR(255) NOT NULL, '
            'sequence_number BIGINT NOT NULL DEFAULT 0, '
            'INDEX session_handoffs_endpoint (host, port)'
            ') ENGINE=InnoDB DEFAULT CHARSET=utf8mb4'
        )
        self.execute(
            'CREATE TABLE IF NOT EXISTS records ('
            'record_id BIGINT AUTO_INCREMENT PRIMARY KEY, '
            'game VARCHAR(64) NOT NULL, '
            'board VARCHAR(64) NOT NULL, '
            'user_id INT NOT NULL, '
            'player VARCHAR(255) NOT NULL, '
            'score BIGINT NOT NULL, '
            'details TEXT NOT NULL, '
            'recorded_at VARCHAR(32) NOT NULL, '
            'INDEX records_board (game, board, score)'
            ') ENGINE=InnoDB DEFAULT CHARSET=utf8mb4'
        )
        self.execute(
            'CREATE TABLE IF NOT EXISTS online_players ('
            'session_id BIGINT UNSIGNED PRIMARY KEY, '
            'game VARCHAR(64) NOT NULL, '
            'user_id INT NOT NULL, '
            'username VARCHAR(255) NOT NULL, '
            'host VARCHAR(255) NOT NULL, '
            'area_id BIGINT NOT NULL, '
            'login_serial BIGINT NOT NULL, '
            'INDEX online_players_game (game, login_serial)'
            ') ENGINE=InnoDB DEFAULT CHARSET=utf8mb4'
        )


def _placeholders(query: str) -> str:
    """Translate the stores' `?` placeholders to PyMySQL's `%s`."""

    return query.replace('?', '%s')
