"""SQLite shared-store backend (default; single machine or a local file)."""

from pathlib import Path
import sqlite3
import threading

from opensnap.storage.sql import Row


class SqliteConnection:
    """Thread-safe SQLite connection implementing `SqlConnection`."""

    integrity_error = sqlite3.IntegrityError

    def __init__(self, path: str | Path) -> None:
        self._lock = threading.Lock()
        self._connection = sqlite3.connect(path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._setup_schema()

    def execute(self, query: str, parameters: tuple[object, ...] = ()) -> None:
        self._write(query, parameters)

    def insert(self, query: str, parameters: tuple[object, ...] = (), *, key: str) -> int:
        # `key` is the table's INTEGER PRIMARY KEY, which SQLite reports as lastrowid.
        del key
        return self._write(query, parameters)

    def insert_or_ignore(self, query: str, parameters: tuple[object, ...] = ()) -> None:
        self._write(query.replace('INSERT INTO', 'INSERT OR IGNORE INTO', 1), parameters)

    def query_one(self, query: str, parameters: tuple[object, ...] = ()) -> Row | None:
        with self._lock:
            return self._connection.execute(query, parameters).fetchone()

    def query_all(self, query: str, parameters: tuple[object, ...] = ()) -> list[Row]:
        with self._lock:
            return self._connection.execute(query, parameters).fetchall()

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def _write(self, query: str, parameters: tuple[object, ...]) -> int:
        with self._lock:
            cursor = self._connection.execute(query, parameters)
            self._connection.commit()
            return int(cursor.lastrowid or 0)

    def _setup_schema(self) -> None:
        self.execute(
            'CREATE TABLE IF NOT EXISTS users ('
            'user_id INTEGER PRIMARY KEY, '
            'username TEXT NOT NULL UNIQUE, '
            'password TEXT NOT NULL, '
            'seed TEXT NOT NULL, '
            "team TEXT NOT NULL DEFAULT ''"
            ')'
        )
        self.execute(
            'CREATE TABLE IF NOT EXISTS session_handoffs ('
            'serial INTEGER PRIMARY KEY AUTOINCREMENT, '
            'session_id INTEGER NOT NULL UNIQUE, '
            'user_id INTEGER NOT NULL, '
            'username TEXT NOT NULL, '
            'host TEXT NOT NULL, '
            'port INTEGER NOT NULL, '
            'game TEXT NOT NULL, '
            'sequence_number INTEGER NOT NULL DEFAULT 0'
            ')'
        )
        self.execute('CREATE INDEX IF NOT EXISTS session_handoffs_endpoint ON session_handoffs (host, port)')
        self.execute(
            'CREATE TABLE IF NOT EXISTS records ('
            'record_id INTEGER PRIMARY KEY AUTOINCREMENT, '
            'game TEXT NOT NULL, '
            'board TEXT NOT NULL, '
            'user_id INTEGER NOT NULL, '
            'player TEXT NOT NULL, '
            'score INTEGER NOT NULL, '
            'details TEXT NOT NULL, '
            'recorded_at TEXT NOT NULL'
            ')'
        )
        self.execute('CREATE INDEX IF NOT EXISTS records_board ON records (game, board, score)')
        # Runtime state (sessions, rooms, lobbies) now lives in each server
        # process; drop the tables older versions kept here.
        for table in ('room_members', 'rooms', 'sessions', 'lobbies'):
            self.execute(f'DROP TABLE IF EXISTS {table}')
