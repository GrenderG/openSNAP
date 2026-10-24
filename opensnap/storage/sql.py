"""Shared stores (accounts, session handoffs, records, online players) over any SQL backend.

Backends provide a `SqlConnection`: `?` placeholders, rows readable by column
name, their own schema and integrity-error type, and the three statements
whose syntax differs between databases (an insert returning its generated key,
an insert that skips rows clashing with a unique key, and an insert that
changes the row it clashes with).
"""

from collections.abc import Mapping
from datetime import UTC, datetime
import json
import time
from typing import Any, Protocol

from opensnap.core.accounts import (
    Account,
    build_account,
    normalize_password_record,
    normalize_seed,
    open_kage_login_key,
    seal_kage_login_key,
)
from opensnap.core.online import OnlinePlayer
from opensnap.core.records import RankedRecord, Ranking, Record
from opensnap.core.sessions import Session, SessionHandoff, create_session_id
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

    def upsert(self, query: str, parameters: tuple[object, ...] = (), *, key: str, update: str) -> None:
        """Run one `INSERT`; a row clashing with the unique columns `key` is changed by the `SET` list `update`.

        In `update`, `excluded.<column>` is the inserted value and
        `<table>.<column>` the stored one. Assignments run left to right on
        MariaDB, each seeing the earlier ones, so a column is assigned only
        after every assignment that reads it.
        """

    def query_one(self, query: str, parameters: tuple[object, ...] = ()) -> Row | None:
        """Run one query and return its first row."""

    def query_all(self, query: str, parameters: tuple[object, ...] = ()) -> list[Row]:
        """Run one query and return all rows."""

    def close(self) -> None:
        """Release the connection."""


def upgrade_password_records(connection: SqlConnection) -> None:
    """Encode accounts still stored with legacy cleartext passwords."""

    for row in connection.query_all('SELECT user_id, password, seed FROM users'):
        seed = normalize_seed(str(row['seed']))
        password_record = normalize_password_record(str(row['password']), seed)
        if password_record != str(row['password']) or seed != str(row['seed']):
            connection.execute(
                'UPDATE users SET password = ?, seed = ? WHERE user_id = ?',
                (password_record, seed, int(row['user_id'])),
            )


class SqlAccountStore:
    """Accounts in the shared store.

    KAGE login keys are stored encrypted with a key derived from the server
    secret (see `seal_kage_login_key`).
    """

    def __init__(self, connection: SqlConnection, *, server_secret: str) -> None:
        self._connection = connection
        self._server_secret = server_secret

    def get_by_name(self, username: str) -> Account | None:
        return self._account_from_row(
            self._connection.query_one(f'SELECT {_ACCOUNT_COLUMNS} FROM users WHERE username = ?', (username,))
        )

    def get_by_id(self, user_id: int) -> Account | None:
        return self._account_from_row(
            self._connection.query_one(f'SELECT {_ACCOUNT_COLUMNS} FROM users WHERE user_id = ?', (user_id,))
        )

    def set_team(self, user_id: int, team: str) -> None:
        self._connection.execute('UPDATE users SET team = ? WHERE user_id = ?', (team, user_id))

    def set_kage_login_key(self, account: Account, key: bytes) -> None:
        self._connection.execute(
            'UPDATE users SET kage_login_key = ? WHERE user_id = ?',
            (seal_kage_login_key(self._server_secret, account.username, key), account.user_id),
        )

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

    def _account_from_row(self, row: Row | None) -> Account | None:
        if row is None:
            return None
        username = str(row['username'])
        return build_account(
            user_id=int(row['user_id']),
            username=username,
            password_record=str(row['password']),
            seed=str(row['seed']),
            team=str(row['team']),
            kage_login_key=open_kage_login_key(self._server_secret, username, row['kage_login_key']),
        )


_ACCOUNT_COLUMNS = 'user_id, username, password, seed, team, kage_login_key'


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

    def get_by_login(self, host: str, username: str) -> SessionHandoff | None:
        return _handoff_from_row(
            self._connection.query_one(
                'SELECT * FROM session_handoffs WHERE host = ? AND username = ? ORDER BY serial DESC',
                (host, username),
            )
        )

    def remove(self, session_id: int) -> None:
        self._connection.execute('DELETE FROM session_handoffs WHERE session_id = ?', (session_id,))


class SqlOnlinePlayerStore:
    """Logged-in players in the shared store (`online_players`)."""

    def __init__(self, connection: SqlConnection) -> None:
        self._connection = connection
        self._last_login_serial = 0

    def login(self, game: str, session: Session) -> None:
        # Strictly increasing, even on coarse clocks: readers order logins and
        # spot repeated ones by it. Each game has one writer, its game server.
        self._last_login_serial = max(time.time_ns(), self._last_login_serial + 1)
        self._connection.insert_or_ignore(
            'INSERT INTO online_players (session_id, game, user_id, username, host, area_id, login_serial) '
            'VALUES (?, ?, ?, ?, ?, 0, 0)',
            (session.session_id, game, session.user_id, session.username, session.endpoint.host),
        )
        self._connection.execute(
            'UPDATE online_players SET host = ?, login_serial = ? WHERE session_id = ?',
            (session.endpoint.host, self._last_login_serial, session.session_id),
        )

    def set_area(self, session_id: int, area_id: int) -> None:
        self._connection.execute('UPDATE online_players SET area_id = ? WHERE session_id = ?', (area_id, session_id))

    def logout(self, session_id: int) -> None:
        self._connection.execute('DELETE FROM online_players WHERE session_id = ?', (session_id,))

    def clear(self, game: str) -> None:
        self._connection.execute('DELETE FROM online_players WHERE game = ?', (game,))

    def list(self, game: str) -> list[OnlinePlayer]:
        rows = self._connection.query_all(
            'SELECT session_id, user_id, username, host, area_id, login_serial FROM online_players '
            'WHERE game = ? ORDER BY login_serial',
            (game,),
        )
        return [
            OnlinePlayer(
                session_id=int(row['session_id']),
                user_id=int(row['user_id']),
                username=str(row['username']),
                host=str(row['host']),
                area_id=int(row['area_id']),
                login_serial=int(row['login_serial']),
            )
            for row in rows
        ]


class SqlRecordStore:
    """Game records in the shared store, one row per game, board and player.

    `sort_key` is the score for lowest-first boards and its negation for the
    others, so one ascending index (`records_rank`) ranks every board.
    """

    def __init__(self, connection: SqlConnection) -> None:
        self._connection = connection

    def submit(
        self,
        *,
        game: str,
        board: str,
        ranking: Ranking,
        user_id: int,
        player: str,
        score: int,
        details: Mapping[str, object] | None = None,
    ) -> None:
        # Microseconds so that players reaching the same score in one second keep their order.
        now = datetime.now(UTC).isoformat(timespec='microseconds')
        sort_key = score if ranking is Ranking.LOWEST else -score
        self._connection.upsert(
            (
                'INSERT INTO records (game, board, user_id, player, score, sort_key, reached_at, details) '
                'VALUES (?, ?, ?, ?, ?, ?, ?, ?)'
            ),
            (game, board, user_id, player, score, sort_key, now, json.dumps(dict(details or {}), sort_keys=True)),
            key='game, board, user_id',
            update=_RECORD_UPDATES[ranking],
        )
        self._connection.insert_or_ignore('INSERT INTO boards (game, board) VALUES (?, ?)', (game, board))

    def top(self, game: str, board: str, limit: int) -> list[Record]:
        rows = self._connection.query_all(
            'SELECT * FROM records WHERE game = ? AND board = ? ORDER BY sort_key, reached_at, user_id LIMIT ?',
            (game, board, limit),
        )
        return [_record_from_row(row) for row in rows]

    def entry(self, game: str, board: str, user_id: int) -> RankedRecord | None:
        # The rank counts the records ahead in `records_rank` order.
        row = self._connection.query_one(
            (
                'SELECT r.*, (SELECT COUNT(*) FROM records a WHERE a.game = r.game AND a.board = r.board '
                'AND a.sort_key <= r.sort_key AND (a.sort_key < r.sort_key OR a.reached_at < r.reached_at '
                'OR (a.reached_at = r.reached_at AND a.user_id < r.user_id))) AS ahead '
                'FROM records r WHERE r.game = ? AND r.board = ? AND r.user_id = ?'
            ),
            (game, board, user_id),
        )
        return None if row is None else RankedRecord(int(row['ahead']) + 1, _record_from_row(row))

    def boards(self, game: str) -> list[str]:
        rows = self._connection.query_all('SELECT board FROM boards WHERE game = ? ORDER BY board', (game,))
        return [str(row['board']) for row in rows]


# Per ranking, how a submission changes the stored record. `reached_at` changes
# only when the shown score does; the best kinds keep the winning
# submission's details.
_IMPROVES = 'excluded.sort_key < records.sort_key'
_BEST_UPDATE = (
    'player = excluded.player, '
    f'score = CASE WHEN {_IMPROVES} THEN excluded.score ELSE records.score END, '
    f'reached_at = CASE WHEN {_IMPROVES} THEN excluded.reached_at ELSE records.reached_at END, '
    f'details = CASE WHEN {_IMPROVES} THEN excluded.details ELSE records.details END, '
    f'sort_key = CASE WHEN {_IMPROVES} THEN excluded.sort_key ELSE records.sort_key END'
)
_RECORD_UPDATES = {
    Ranking.LATEST: (
        'player = excluded.player, details = excluded.details, '
        'reached_at = CASE WHEN excluded.score = records.score THEN records.reached_at ELSE excluded.reached_at END, '
        'score = excluded.score, sort_key = excluded.sort_key'
    ),
    Ranking.LOWEST: _BEST_UPDATE,
    Ranking.HIGHEST: _BEST_UPDATE,
    Ranking.TOTAL: (
        'player = excluded.player, details = excluded.details, '
        'reached_at = CASE WHEN excluded.score = 0 THEN records.reached_at ELSE excluded.reached_at END, '
        'score = records.score + excluded.score, sort_key = records.sort_key + excluded.sort_key'
    ),
}


def _record_from_row(row: Row) -> Record:
    return Record(
        game=str(row['game']),
        board=str(row['board']),
        user_id=int(row['user_id']),
        player=str(row['player']),
        score=int(row['score']),
        details=json.loads(str(row['details'])),
        reached_at=str(row['reached_at']),
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
