"""Shared stores (accounts, session handoffs, records) over any SQL backend.

Backends provide a `SqlConnection`: `?` placeholders, rows readable by column
name, their own schema and integrity-error type, and the two statements whose
syntax differs between databases (an insert returning its generated key, and
an insert that skips rows clashing with a unique key).
"""

from collections.abc import Mapping
from datetime import UTC, datetime
import json
from typing import Any, Protocol

from opensnap.config import UserConfig
from opensnap.core.accounts import (
    Account,
    build_account,
    normalize_password_record,
    normalize_seed,
)
from opensnap.core.records import PlayerTotal, Record
from opensnap.core.sessions import SessionHandoff, create_session_id
from opensnap.protocol.models import Endpoint
from opensnap.storage.interfaces import DuplicateAccountError

Row = Mapping[str, Any]


class SqlConnection(Protocol):
    """Minimal SQL backend contract used by the shared stores."""

    integrity_error: type[Exception]

    def execute(self, query: str, parameters: tuple[object, ...] = ()) -> None:
        """Run one write statement."""

    def insert(self, query: str, parameters: tuple[object, ...] = (), *, key: str) -> int:
        """Run one `INSERT` and return the value generated for column `key`."""

    def insert_or_ignore(self, query: str, parameters: tuple[object, ...] = ()) -> None:
        """Run one `INSERT`, skipping rows that clash with a unique key."""

    def query_one(self, query: str, parameters: tuple[object, ...] = ()) -> Row | None:
        """Run one query and return its first row."""

    def query_all(self, query: str, parameters: tuple[object, ...] = ()) -> list[Row]:
        """Run one query and return all rows."""

    def close(self) -> None:
        """Release the connection."""


def seed_users(connection: SqlConnection, users: tuple[UserConfig, ...]) -> None:
    """Insert configured default users and encode legacy cleartext passwords."""

    for user in users:
        seed = normalize_seed(user.seed)
        connection.insert_or_ignore(
            'INSERT INTO users (username, password, seed, team) VALUES (?, ?, ?, ?)',
            (user.username, normalize_password_record(user.password, seed), seed, user.team),
        )
    for row in connection.query_all('SELECT user_id, password, seed FROM users'):
        seed = normalize_seed(str(row['seed']))
        password_record = normalize_password_record(str(row['password']), seed)
        if password_record != str(row['password']) or seed != str(row['seed']):
            connection.execute(
                'UPDATE users SET password = ?, seed = ? WHERE user_id = ?',
                (password_record, seed, int(row['user_id'])),
            )


class SqlAccountStore:
    """Accounts in the shared store."""

    def __init__(self, connection: SqlConnection) -> None:
        self._connection = connection

    def get_by_name(self, username: str) -> Account | None:
        return _account_from_row(
            self._connection.query_one(
                'SELECT user_id, username, password, seed, team FROM users WHERE username = ?',
                (username,),
            )
        )

    def get_by_id(self, user_id: int) -> Account | None:
        return _account_from_row(
            self._connection.query_one(
                'SELECT user_id, username, password, seed, team FROM users WHERE user_id = ?',
                (user_id,),
            )
        )

    def set_team(self, user_id: int, team: str) -> None:
        self._connection.execute('UPDATE users SET team = ? WHERE user_id = ?', (team, user_id))

    def create_user(self, username: str, password: str) -> Account:
        seed = normalize_seed('')
        password_record = normalize_password_record(password, seed)
        try:
            user_id = self._connection.insert(
                'INSERT INTO users (username, password, seed, team) VALUES (?, ?, ?, ?)',
                (username, password_record, seed, ''),
                key='user_id',
            )
        except self._connection.integrity_error as exc:
            raise DuplicateAccountError(username) from exc
        return build_account(user_id=user_id, username=username, password_record=password_record, seed=seed, team='')


class SqlSessionHandoffStore:
    """Bootstrap-to-game session handoffs in the shared store."""

    def __init__(self, connection: SqlConnection) -> None:
        self._connection = connection

    def issue(self, endpoint: Endpoint, account: Account, *, game_identifier: str) -> SessionHandoff:
        session_id = create_session_id(endpoint.host, account)
        self._connection.execute(
            'DELETE FROM session_handoffs WHERE session_id = ? OR (host = ? AND port = ?)',
            (session_id, endpoint.host, endpoint.port),
        )
        serial = self._connection.insert(
            (
                'INSERT INTO session_handoffs '
                '(session_id, user_id, username, host, port, game, sequence_number) '
                'VALUES (?, ?, ?, ?, ?, ?, 0)'
            ),
            (session_id, account.user_id, account.username, endpoint.host, endpoint.port, game_identifier),
            key='serial',
        )
        return SessionHandoff(
            serial=serial,
            session_id=session_id,
            user_id=account.user_id,
            username=account.username,
            endpoint=endpoint,
            game_identifier=game_identifier,
        )

    def set_sequence(self, session_id: int, sequence_number: int) -> None:
        self._connection.execute(
            'UPDATE session_handoffs SET sequence_number = ? WHERE session_id = ?',
            (sequence_number, session_id),
        )

    def get(self, session_id: int) -> SessionHandoff | None:
        return _handoff_from_row(
            self._connection.query_one('SELECT * FROM session_handoffs WHERE session_id = ?', (session_id,))
        )

    def get_by_endpoint(self, endpoint: Endpoint) -> SessionHandoff | None:
        return _handoff_from_row(
            self._connection.query_one(
                'SELECT * FROM session_handoffs WHERE host = ? AND port = ?',
                (endpoint.host, endpoint.port),
            )
        )

    def remove(self, session_id: int) -> None:
        self._connection.execute('DELETE FROM session_handoffs WHERE session_id = ?', (session_id,))


class SqlRecordStore:
    """Game records in the shared store, one row per submission."""

    def __init__(self, connection: SqlConnection) -> None:
        self._connection = connection

    def add(
        self,
        *,
        game: str,
        board: str,
        user_id: int,
        player: str,
        score: int,
        details: Mapping[str, object],
    ) -> Record:
        details_text = json.dumps(dict(details), sort_keys=True)
        recorded_at = datetime.now(UTC).isoformat(timespec='seconds')
        record_id = self._connection.insert(
            (
                'INSERT INTO records (game, board, user_id, player, score, details, recorded_at) '
                'VALUES (?, ?, ?, ?, ?, ?, ?)'
            ),
            (game, board, user_id, player, score, details_text, recorded_at),
            key='record_id',
        )
        return Record(
            record_id=record_id,
            game=game,
            board=board,
            user_id=user_id,
            player=player,
            score=score,
            details=json.loads(details_text),
            recorded_at=recorded_at,
        )

    def best_by_board(self, game: str, limit: int) -> dict[str, list[Record]]:
        rows = self._connection.query_all(
            (
                'SELECT r.* FROM records r JOIN ('
                'SELECT board, user_id, MIN(score) AS best FROM records WHERE game = ? GROUP BY board, user_id'
                ') b ON r.board = b.board AND r.user_id = b.user_id AND r.score = b.best '
                'WHERE r.game = ? ORDER BY r.board, r.score, r.record_id'
            ),
            (game, game),
        )
        boards: dict[str, list[Record]] = {}
        seen: set[tuple[str, int]] = set()
        for row in rows:
            record = _record_from_row(row)
            # A player's best score may appear more than once; keep the earliest.
            if (record.board, record.user_id) in seen:
                continue
            seen.add((record.board, record.user_id))
            board = boards.setdefault(record.board, [])
            if len(board) < limit:
                board.append(record)
        return boards

    def totals(self, game: str, board_prefix: str, limit: int) -> list[PlayerTotal]:
        rows = self._connection.query_all(
            (
                'SELECT user_id, MAX(player) AS player, SUM(score) AS total FROM records '
                'WHERE game = ? AND SUBSTR(board, 1, ?) = ? '
                'GROUP BY user_id ORDER BY total DESC, user_id LIMIT ?'
            ),
            (game, len(board_prefix), board_prefix, limit),
        )
        return [PlayerTotal(int(row['user_id']), str(row['player']), int(row['total'])) for row in rows]


def _record_from_row(row: Row) -> Record:
    return Record(
        record_id=int(row['record_id']),
        game=str(row['game']),
        board=str(row['board']),
        user_id=int(row['user_id']),
        player=str(row['player']),
        score=int(row['score']),
        details=json.loads(str(row['details'])),
        recorded_at=str(row['recorded_at']),
    )


def _account_from_row(row: Row | None) -> Account | None:
    if row is None:
        return None
    return build_account(
        user_id=int(row['user_id']),
        username=str(row['username']),
        password_record=str(row['password']),
        seed=str(row['seed']),
        team=str(row['team']),
    )


def _handoff_from_row(row: Row | None) -> SessionHandoff | None:
    if row is None:
        return None
    return SessionHandoff(
        serial=int(row['serial']),
        session_id=int(row['session_id']),
        user_id=int(row['user_id']),
        username=str(row['username']),
        endpoint=Endpoint(host=str(row['host']), port=int(row['port'])),
        game_identifier=str(row['game']),
        sequence_number=int(row['sequence_number']),
    )
