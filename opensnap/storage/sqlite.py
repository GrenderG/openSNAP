"""SQLite storage backend."""

import sqlite3

from opensnap.config import LobbyConfig, UserConfig
from opensnap.core.accounts import Account
from opensnap.core.lobbies import Lobby
from opensnap.core.rooms import GameRoom
from opensnap.core.sessions import Session, create_session_id
from opensnap.protocol.constants import FLAG_RELIABLE
from opensnap.protocol.models import Endpoint


class SqliteDatabase:
    """Shared SQLite connection and schema lifecycle."""

    def __init__(self, path: str) -> None:
        self._connection = sqlite3.connect(path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute('PRAGMA foreign_keys = ON')
        self._setup_schema()

    def execute(self, query: str, parameters: tuple[object, ...] = ()) -> sqlite3.Cursor:
        """Execute write query and commit."""

        cursor = self._connection.execute(query, parameters)
        self._connection.commit()
        return cursor

    def query_one(self, query: str, parameters: tuple[object, ...] = ()) -> sqlite3.Row | None:
        """Execute query and fetch one row."""

        return self._connection.execute(query, parameters).fetchone()

    def query_all(self, query: str, parameters: tuple[object, ...] = ()) -> list[sqlite3.Row]:
        """Execute query and fetch all rows."""

        return self._connection.execute(query, parameters).fetchall()

    def seed(self, users: tuple[UserConfig, ...], lobbies: tuple[LobbyConfig, ...]) -> None:
        """Seed static accounts and lobbies."""

        for user in users:
            self.execute(
                (
                    'INSERT OR IGNORE INTO users '
                    '(user_id, username, password, seed, team) VALUES (?, ?, ?, ?, ?)'
                ),
                (user.user_id, user.username, user.password, user.seed, user.team),
            )

        for lobby in lobbies:
            self.execute(
                'INSERT OR IGNORE INTO lobbies (lobby_id, name) VALUES (?, ?)',
                (lobby.lobby_id, lobby.name),
            )

    def _setup_schema(self) -> None:
        """Create required tables."""

        self.execute(
            (
                'CREATE TABLE IF NOT EXISTS users ('
                'user_id INTEGER PRIMARY KEY, '
                'username TEXT NOT NULL UNIQUE, '
                'password TEXT NOT NULL, '
                'seed TEXT NOT NULL, '
                'team TEXT NOT NULL DEFAULT ""'
                ')'
            )
        )
        self.execute(
            (
                'CREATE TABLE IF NOT EXISTS lobbies ('
                'lobby_id INTEGER PRIMARY KEY, '
                'name TEXT NOT NULL'
                ')'
            )
        )
        self.execute(
            (
                'CREATE TABLE IF NOT EXISTS sessions ('
                'session_id INTEGER PRIMARY KEY, '
                'user_id INTEGER NOT NULL, '
                'username TEXT NOT NULL, '
                'host TEXT NOT NULL, '
                'port INTEGER NOT NULL, '
                'request_number INTEGER NOT NULL DEFAULT 0, '
                'sequence_number INTEGER NOT NULL DEFAULT 0, '
                'lobby_id INTEGER NOT NULL DEFAULT 0, '
                'room_id INTEGER NOT NULL DEFAULT 0'
                ')'
            )
        )
        self.execute(
            (
                'CREATE TABLE IF NOT EXISTS rooms ('
                'room_id INTEGER PRIMARY KEY AUTOINCREMENT, '
                'name TEXT NOT NULL, '
                'password TEXT NOT NULL, '
                'rules INTEGER NOT NULL, '
                'max_players INTEGER NOT NULL, '
                'lobby_id INTEGER NOT NULL, '
                'host_session_id INTEGER NOT NULL'
                ')'
            )
        )
        self.execute(
            (
                'CREATE TABLE IF NOT EXISTS room_members ('
                'room_id INTEGER NOT NULL, '
                'session_id INTEGER NOT NULL, '
                'PRIMARY KEY (room_id, session_id), '
                'FOREIGN KEY(room_id) REFERENCES rooms(room_id) ON DELETE CASCADE'
                ')'
            )
        )


class SqliteAccountDirectory:
    """SQLite account directory."""

    def __init__(self, database: SqliteDatabase) -> None:
        self._database = database

    def get_by_name(self, username: str) -> Account | None:
        """Get account by username."""

        row = self._database.query_one(
            'SELECT user_id, username, password, seed, team FROM users WHERE username = ?',
            (username,),
        )
        return _account_from_row(row)

    def get_by_id(self, user_id: int) -> Account | None:
        """Get account by user id."""

        row = self._database.query_one(
            'SELECT user_id, username, password, seed, team FROM users WHERE user_id = ?',
            (user_id,),
        )
        return _account_from_row(row)

    def set_team(self, user_id: int, team: str) -> None:
        """Set account team."""

        self._database.execute('UPDATE users SET team = ? WHERE user_id = ?', (team, user_id))


class SqliteSessionRegistry:
    """SQLite-backed session registry."""

    def __init__(self, database: SqliteDatabase) -> None:
        self._database = database

    def create_or_replace(self, endpoint: Endpoint, account: Account) -> Session:
        """Create or replace session for a user endpoint."""

        session_id = create_session_id(endpoint.host, account)
        self._database.execute('DELETE FROM sessions WHERE session_id = ?', (session_id,))
        self._database.execute(
            'DELETE FROM sessions WHERE host = ? AND port = ?',
            (endpoint.host, endpoint.port),
        )
        self._database.execute(
            (
                'INSERT INTO sessions '
                '(session_id, user_id, username, host, port, request_number, sequence_number, lobby_id, room_id) '
                'VALUES (?, ?, ?, ?, ?, 0, 0, 0, 0)'
            ),
            (session_id, account.user_id, account.username, endpoint.host, endpoint.port),
        )
        return Session(
            session_id=session_id,
            user_id=account.user_id,
            username=account.username,
            endpoint=endpoint,
        )

    def get(self, session_id: int) -> Session | None:
        """Get session by id."""

        row = self._database.query_one('SELECT * FROM sessions WHERE session_id = ?', (session_id,))
        return _session_from_row(row)

    def get_by_endpoint(self, endpoint: Endpoint) -> Session | None:
        """Get session by endpoint."""

        row = self._database.query_one(
            'SELECT * FROM sessions WHERE host = ? AND port = ?',
            (endpoint.host, endpoint.port),
        )
        return _session_from_row(row)

    def is_valid(self, session_id: int) -> bool:
        """Check whether session exists."""

        row = self._database.query_one(
            'SELECT COUNT(*) AS count FROM sessions WHERE session_id = ?',
            (session_id,),
        )
        return bool(row and row['count'] > 0)

    def allocate_sequence(self, session_id: int, type_flags: int) -> int:
        """Allocate outbound sequence number."""

        row = self._database.query_one(
            'SELECT request_number, sequence_number FROM sessions WHERE session_id = ?',
            (session_id,),
        )
        if row is None:
            return 0

        if type_flags & FLAG_RELIABLE:
            value = row['request_number']
            self._database.execute(
                'UPDATE sessions SET request_number = request_number + 1 WHERE session_id = ?',
                (session_id,),
            )
            return int(value)

        value = row['sequence_number'] + 1
        self._database.execute(
            'UPDATE sessions SET sequence_number = ? WHERE session_id = ?',
            (value, session_id),
        )
        return int(value)

    def set_lobby(self, session_id: int, lobby_id: int) -> None:
        """Set current lobby for a session."""

        self._database.execute(
            'UPDATE sessions SET lobby_id = ? WHERE session_id = ?',
            (lobby_id, session_id),
        )

    def set_room(self, session_id: int, room_id: int) -> None:
        """Set current room for a session."""

        self._database.execute(
            'UPDATE sessions SET room_id = ? WHERE session_id = ?',
            (room_id, session_id),
        )

    def count_users_in_lobby(self, lobby_id: int) -> int:
        """Count users in lobby."""

        row = self._database.query_one(
            'SELECT COUNT(*) AS count FROM sessions WHERE lobby_id = ?',
            (lobby_id,),
        )
        if row is None:
            return 0
        return int(row['count'])

    def list_room_members(self, room_id: int) -> list[Session]:
        """List room members."""

        rows = self._database.query_all(
            'SELECT * FROM sessions WHERE room_id = ?',
            (room_id,),
        )
        return [_session_from_row(row) for row in rows if row is not None]

    def endpoint_for_session(self, session_id: int) -> Endpoint | None:
        """Get endpoint for session id."""

        row = self._database.query_one(
            'SELECT host, port FROM sessions WHERE session_id = ?',
            (session_id,),
        )
        if row is None:
            return None
        return Endpoint(host=str(row['host']), port=int(row['port']))


class SqliteLobbyRegistry:
    """SQLite lobby registry."""

    def __init__(self, database: SqliteDatabase) -> None:
        self._database = database

    def list(self) -> list[Lobby]:
        """List lobbies sorted by id."""

        rows = self._database.query_all('SELECT lobby_id, name FROM lobbies ORDER BY lobby_id')
        return [Lobby(lobby_id=int(row['lobby_id']), name=str(row['name'])) for row in rows]

    def get(self, lobby_id: int) -> Lobby | None:
        """Get lobby by id."""

        row = self._database.query_one(
            'SELECT lobby_id, name FROM lobbies WHERE lobby_id = ?',
            (lobby_id,),
        )
        if row is None:
            return None
        return Lobby(lobby_id=int(row['lobby_id']), name=str(row['name']))


class SqliteRoomRegistry:
    """SQLite game room registry."""

    def __init__(self, database: SqliteDatabase) -> None:
        self._database = database

    def create_room(
        self,
        *,
        name: str,
        password: str,
        rules: int,
        max_players: int,
        lobby_id: int,
        host_session_id: int,
    ) -> GameRoom:
        """Create room and add host as first member."""

        cursor = self._database.execute(
            (
                'INSERT INTO rooms '
                '(name, password, rules, max_players, lobby_id, host_session_id) '
                'VALUES (?, ?, ?, ?, ?, ?)'
            ),
            (name, password, rules, max_players, lobby_id, host_session_id),
        )
        room_id = int(cursor.lastrowid)
        self._database.execute(
            'INSERT OR IGNORE INTO room_members (room_id, session_id) VALUES (?, ?)',
            (room_id, host_session_id),
        )
        return self.get(room_id) or GameRoom(
            room_id=room_id,
            name=name,
            password=password,
            rules=rules,
            max_players=max_players,
            lobby_id=lobby_id,
            host_session_id=host_session_id,
            members={host_session_id},
        )

    def get(self, room_id: int) -> GameRoom | None:
        """Get room by id."""

        row = self._database.query_one('SELECT * FROM rooms WHERE room_id = ?', (room_id,))
        return self._room_from_row(row)

    def list_for_lobby(self, lobby_id: int) -> list[GameRoom]:
        """List rooms in lobby."""

        rows = self._database.query_all(
            'SELECT * FROM rooms WHERE lobby_id = ? ORDER BY room_id',
            (lobby_id,),
        )
        return [room for row in rows if (room := self._room_from_row(row)) is not None]

    def join(self, room_id: int, session_id: int) -> bool:
        """Join room if capacity allows."""

        room = self.get(room_id)
        if room is None:
            return False

        if session_id in room.members:
            return True

        if len(room.members) >= room.max_players:
            return False

        self._database.execute(
            'INSERT OR IGNORE INTO room_members (room_id, session_id) VALUES (?, ?)',
            (room_id, session_id),
        )
        return True

    def leave(self, room_id: int, session_id: int) -> None:
        """Leave room and remove empty rooms."""

        self._database.execute(
            'DELETE FROM room_members WHERE room_id = ? AND session_id = ?',
            (room_id, session_id),
        )
        row = self._database.query_one(
            'SELECT COUNT(*) AS count FROM room_members WHERE room_id = ?',
            (room_id,),
        )
        if row is not None and int(row['count']) == 0:
            self._database.execute('DELETE FROM rooms WHERE room_id = ?', (room_id,))

    def _room_from_row(self, row: sqlite3.Row | None) -> GameRoom | None:
        """Convert database row to room model."""

        if row is None:
            return None

        member_rows = self._database.query_all(
            'SELECT session_id FROM room_members WHERE room_id = ?',
            (int(row['room_id']),),
        )
        members = {int(member['session_id']) for member in member_rows}
        return GameRoom(
            room_id=int(row['room_id']),
            name=str(row['name']),
            password=str(row['password']),
            rules=int(row['rules']),
            max_players=int(row['max_players']),
            lobby_id=int(row['lobby_id']),
            host_session_id=int(row['host_session_id']),
            members=members,
        )


def _account_from_row(row: sqlite3.Row | None) -> Account | None:
    """Convert row to account model."""

    if row is None:
        return None
    return Account(
        user_id=int(row['user_id']),
        username=str(row['username']),
        password=str(row['password']),
        seed=str(row['seed']),
        team=str(row['team']),
    )


def _session_from_row(row: sqlite3.Row | None) -> Session | None:
    """Convert row to session model."""

    if row is None:
        return None

    return Session(
        session_id=int(row['session_id']),
        user_id=int(row['user_id']),
        username=str(row['username']),
        endpoint=Endpoint(host=str(row['host']), port=int(row['port'])),
        request_number=int(row['request_number']),
        sequence_number=int(row['sequence_number']),
        lobby_id=int(row['lobby_id']),
        room_id=int(row['room_id']),
    )
