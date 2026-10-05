"""Storage backend protocols.

Shared stores (accounts, session handoffs, records) live in the configured
backend and may be shared by the web, bootstrap and game servers across
machines. Runtime
stores (sessions with transport counters, lobbies, rooms) belong to one
process and are kept in memory.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Callable
from typing import Protocol

from opensnap.core.accounts import Account
from opensnap.core.lobbies import Lobby
from opensnap.core.records import PlayerTotal, Record
from opensnap.core.rooms import GameRoom
from opensnap.core.sessions import Session, SessionHandoff
from opensnap.protocol.models import Endpoint


class AccountStore(Protocol):
    """Shared account store interface."""

    def get_by_name(self, username: str) -> Account | None:
        """Get account by username."""

    def get_by_id(self, user_id: int) -> Account | None:
        """Get account by id."""

    def set_team(self, user_id: int, team: str) -> None:
        """Set user team."""

    def create_user(self, username: str, password: str) -> Account:
        """Create an account; raises `DuplicateAccountError` if the name exists."""


class SessionHandoffStore(Protocol):
    """Shared bootstrap-to-game session handoff interface."""

    def issue(self, endpoint: Endpoint, account: Account, *, game_identifier: str) -> SessionHandoff:
        """Record a new login, replacing earlier handoffs of the session or endpoint."""

    def set_sequence(self, session_id: int, sequence_number: int) -> None:
        """Record the bootstrap's last unreliable sequence for one session."""

    def get(self, session_id: int) -> SessionHandoff | None:
        """Get handoff by session id."""

    def get_by_endpoint(self, endpoint: Endpoint) -> SessionHandoff | None:
        """Get handoff by client endpoint."""

    def remove(self, session_id: int) -> None:
        """Remove one handoff."""


class RecordStore(Protocol):
    """Shared game records interface; every submission is kept."""

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
        """Store one submitted record."""

    def best_by_board(self, game: str, limit: int) -> dict[str, list[Record]]:
        """Return each board's lowest score per player, best first, at most `limit` players."""

    def totals(self, game: str, board_prefix: str, limit: int) -> list[PlayerTotal]:
        """Return players by summed score over the boards starting with `board_prefix`, highest first."""


class SessionStore(Protocol):
    """Runtime (in-process) session store interface."""

    def adopt(self, handoff: SessionHandoff) -> Session:
        """Create or replace the runtime session of one handoff."""

    def rebind_endpoint(self, session_id: int, endpoint: Endpoint) -> Session | None:
        """Bind an existing session id to a new endpoint."""

    def remove(self, session_id: int) -> None:
        """Remove one session and any direct endpoint binding."""

    def get(self, session_id: int) -> Session | None:
        """Get session by id."""

    def get_by_endpoint(self, endpoint: Endpoint) -> Session | None:
        """Get session by endpoint."""

    def is_valid(self, session_id: int) -> bool:
        """Check if session exists."""

    def allocate_sequence(self, session_id: int, type_flags: int) -> int:
        """Allocate outbound sequence number."""

    def accept_incoming(self, session_id: int, sequence_number: int) -> bool:
        """Accept or reject incoming sequence number for a session."""

    def set_lobby(self, session_id: int, lobby_id: int) -> None:
        """Set lobby for session."""

    def set_room(self, session_id: int, room_id: int) -> None:
        """Set room for session."""

    def count_users_in_lobby(self, lobby_id: int) -> int:
        """Count users in lobby."""

    def list_lobby_members(self, lobby_id: int) -> list[Session]:
        """List lobby members."""

    def list_room_members(self, room_id: int) -> list[Session]:
        """List room members."""

    def endpoint_for_session(self, session_id: int) -> Endpoint | None:
        """Get endpoint for session."""


class LobbyStore(Protocol):
    """Lobby store interface."""

    def list(self) -> list[Lobby]:
        """List lobbies."""

    def get(self, lobby_id: int) -> Lobby | None:
        """Get lobby by id."""


class RoomStore(Protocol):
    """Room store interface."""

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
        """Create game room."""

    def get(self, room_id: int) -> GameRoom | None:
        """Get room by id."""

    def list_for_lobby(self, lobby_id: int) -> list[GameRoom]:
        """List rooms for lobby."""

    def join(self, room_id: int, session_id: int) -> bool:
        """Join room."""

    def leave(self, room_id: int, session_id: int) -> None:
        """Leave room."""

    def set_rules(self, room_id: int, rules: int) -> None:
        """Replace the room rules word (`CMD_CHANGE_ATTRIBUTE` `STAT`)."""


class DuplicateAccountError(Exception):
    """Raised when creating an account whose username already exists."""


@dataclass(slots=True)
class StorageBundle:
    """Resolved shared storage backend."""

    accounts: AccountStore
    handoffs: SessionHandoffStore
    records: RecordStore
    _close: Callable[[], None]

    def close(self) -> None:
        """Close backend resources owned by this bundle."""

        self._close()
