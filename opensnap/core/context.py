"""Handler context and response helpers."""

from collections.abc import Callable
from dataclasses import dataclass

from opensnap.config import AppConfig
from opensnap.core.sessions import Session
from opensnap.protocol import commands
from opensnap.protocol.models import Endpoint, SnapMessage
from opensnap.storage.interfaces import (
    AccountStore,
    LobbyStore,
    RecordStore,
    RoomStore,
    SessionHandoffStore,
    SessionStore,
)


@dataclass(slots=True)
class HandlerContext:
    """Shared runtime services exposed to command handlers."""

    config: AppConfig
    accounts: AccountStore
    handoffs: SessionHandoffStore
    records: RecordStore
    sessions: SessionStore
    lobbies: LobbyStore
    rooms: RoomStore
    # `(title code, footer marker)` -> `(game identifier, title name)` for
    # bootstrap logins, or None for an unknown client build.
    identify_snap_title: Callable[[int, int], tuple[str, str] | None]
    # Tear one session down as a transport timeout does (plugin cleanup,
    # rooms, handoff) and return the callbacks it produces for other players.
    end_session: Callable[[Session], list[SnapMessage]]

    def reply(
        self,
        request: SnapMessage,
        *,
        type_flags: int,
        command: int,
        payload: bytes = b'',
        endpoint: Endpoint | None = None,
        session_id: int | None = None,
        packet_number: int | None = None,
        acknowledge_number: int | None = None,
        size_word_override: int | None = None,
    ) -> SnapMessage:
        """Create response message bound to a request context."""

        outbound_endpoint = endpoint or request.endpoint
        outbound_session_id = request.session_id if session_id is None else session_id

        if acknowledge_number is None:
            acknowledge_number = request.sequence_number

        if command == commands.CMD_ACK and not payload:
            # Release and beta1 both build standalone transport ACKs as bare
            # response packets with packet/sequence numbers cleared; only the
            # ACK field carries state for reverse-ACK retirement.
            sequence_number = 0
            packet_number = 0
        else:
            sequence_number = self.sessions.allocate_sequence(outbound_session_id, type_flags)

        if packet_number is None:
            packet_number = request.packet_number

        return SnapMessage(
            endpoint=outbound_endpoint,
            type_flags=type_flags,
            packet_number=packet_number,
            command=command,
            session_id=outbound_session_id,
            sequence_number=sequence_number,
            acknowledge_number=acknowledge_number,
            payload=payload,
            size_word_override=size_word_override,
        )

    def direct(
        self,
        *,
        endpoint: Endpoint,
        session_id: int,
        type_flags: int,
        command: int,
        payload: bytes = b'',
        packet_number: int = 0,
        acknowledge_number: int = 0,
        size_word_override: int | None = None,
        source_session_id: int | None = None,
    ) -> SnapMessage:
        """Create message that is not tied to one request endpoint.

        `session_id` is the recipient connection; `source_session_id` optionally
        overrides the header session word for relays that identify a peer.
        """

        sequence_number = self.sessions.allocate_sequence(session_id, type_flags)
        return SnapMessage(
            endpoint=endpoint,
            type_flags=type_flags,
            packet_number=packet_number,
            command=command,
            session_id=session_id,
            sequence_number=sequence_number,
            acknowledge_number=acknowledge_number,
            payload=payload,
            size_word_override=size_word_override,
            source_session_id=source_session_id,
        )
