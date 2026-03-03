"""Session lifecycle and counters."""

import hashlib
from dataclasses import dataclass

from opensnap.core.accounts import Account
from opensnap.protocol.constants import FLAG_RELIABLE
from opensnap.protocol.models import Endpoint


@dataclass(slots=True)
class Session:
    """Connected client session state."""

    session_id: int
    user_id: int
    username: str
    endpoint: Endpoint
    game_plugin: str = ''
    request_number: int = 0
    sequence_number: int = 0
    last_incoming_sequence: int = -1
    lobby_id: int = 0
    room_id: int = 0


class SessionRegistry:
    """In-memory session registry."""

    def __init__(self) -> None:
        self._by_id: dict[int, Session] = {}
        self._id_by_endpoint: dict[Endpoint, int] = {}

    def create_or_replace(
        self,
        endpoint: Endpoint,
        account: Account,
        *,
        game_identifier: str = '',
    ) -> Session:
        """Create or replace session for a user endpoint."""

        session_id = create_session_id(endpoint.host, account)
        existing = self._by_id.get(session_id)
        if existing is not None:
            self._id_by_endpoint.pop(existing.endpoint, None)

        session = Session(
            session_id=session_id,
            user_id=account.user_id,
            username=account.username,
            endpoint=endpoint,
            game_plugin=game_identifier,
        )
        self._by_id[session_id] = session
        self._id_by_endpoint[endpoint] = session_id
        return session

    def rebind_endpoint(self, session_id: int, endpoint: Endpoint) -> Session | None:
        """Bind an existing session to a new client endpoint."""

        session = self._by_id.get(session_id)
        if session is None:
            return None

        self._id_by_endpoint.pop(session.endpoint, None)
        self._id_by_endpoint.pop(endpoint, None)
        session.endpoint = endpoint
        self._id_by_endpoint[endpoint] = session_id
        return session

    def get(self, session_id: int) -> Session | None:
        """Get session by id."""

        return self._by_id.get(session_id)

    def get_by_endpoint(self, endpoint: Endpoint) -> Session | None:
        """Get session by endpoint."""

        session_id = self._id_by_endpoint.get(endpoint)
        if session_id is None:
            return None
        return self._by_id.get(session_id)

    def is_valid(self, session_id: int) -> bool:
        """Check whether session exists."""

        return session_id in self._by_id

    def allocate_sequence(self, session_id: int, type_flags: int) -> int:
        """Allocate outgoing sequence number based on type flags."""

        session = self._by_id.get(session_id)
        if session is None:
            return 0

        if type_flags & FLAG_RELIABLE:
            value = session.request_number
            session.request_number += 1
            return value

        session.sequence_number += 1
        return session.sequence_number

    def accept_incoming(self, session_id: int, sequence_number: int) -> bool:
        """Accept or reject an incoming sequence number."""

        session = self._by_id.get(session_id)
        if session is None:
            return False

        if sequence_number <= session.last_incoming_sequence:
            return False

        session.last_incoming_sequence = sequence_number
        return True

    def set_lobby(self, session_id: int, lobby_id: int) -> None:
        """Set current lobby for a session."""

        session = self._by_id.get(session_id)
        if session is not None:
            session.lobby_id = lobby_id

    def set_room(self, session_id: int, room_id: int) -> None:
        """Set current room for a session."""

        session = self._by_id.get(session_id)
        if session is not None:
            session.room_id = room_id

    def count_users_in_lobby(self, lobby_id: int) -> int:
        """Count connected sessions currently in a lobby."""

        return sum(1 for session in self._by_id.values() if session.lobby_id == lobby_id)

    def list_lobby_members(self, lobby_id: int) -> list[Session]:
        """List sessions currently in a lobby."""

        return [session for session in self._by_id.values() if session.lobby_id == lobby_id]

    def list_room_members(self, room_id: int) -> list[Session]:
        """List sessions currently in a room."""

        return [session for session in self._by_id.values() if session.room_id == room_id]

    def endpoint_for_session(self, session_id: int) -> Endpoint | None:
        """Get endpoint for a known session."""

        session = self._by_id.get(session_id)
        if session is None:
            return None
        return session.endpoint


def create_session_id(host: str, account: Account) -> int:
    """Create deterministic session id compatible with observed behavior."""

    digest = hashlib.md5()
    digest.update(host.encode('utf-8'))
    digest.update(account.username.encode('utf-8'))
    digest.update(account.session_material.encode('utf-8'))
    return int(digest.hexdigest()[:8], 16)
