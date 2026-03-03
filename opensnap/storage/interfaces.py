"""Storage backend protocols."""

from dataclasses import dataclass
from typing import Callable
from typing import Protocol

from opensnap.core.accounts import Account
from opensnap.core.lobbies import Lobby
from opensnap.core.rooms import GameRoom
from opensnap.core.sessions import Session
from opensnap.protocol.models import Endpoint


class AccountStore(Protocol):
    """Account store interface."""

    def get_by_name(self, username: str) -> Account | None:
        """Get account by username."""

    def get_by_id(self, user_id: int) -> Account | None:
        """Get account by id."""

    def set_team(self, user_id: int, team: str) -> None:
        """Set user team."""


class SessionStore(Protocol):
    """Session store interface."""

    def create_or_replace(
        self,
        endpoint: Endpoint,
        account: Account,
        *,
        game_identifier: str = '',
    ) -> Session:
        """Create or replace session."""

    def rebind_endpoint(self, session_id: int, endpoint: Endpoint) -> Session | None:
        """Bind an existing session id to a new endpoint."""

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


@dataclass(slots=True)
class StorageBundle:
    """Resolved storage backend set."""

    accounts: AccountStore
    sessions: SessionStore
    lobbies: LobbyStore
    rooms: RoomStore
    _close: Callable[[], None]

    def close(self) -> None:
        """Close backend resources owned by this bundle."""

        self._close()
