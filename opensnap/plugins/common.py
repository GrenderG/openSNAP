"""Game-agnostic SNAP helpers shared by game plugins."""

import logging
import struct

from opensnap.core.context import HandlerContext
from opensnap.core.sessions import Session
from opensnap.protocol import commands
from opensnap.protocol.constants import FLAG_RESPONSE, FLAG_ROOM
from opensnap.protocol.models import SnapMessage


# Binary-verified attribute selector used by kkQueryLobbyAttribute/kkQueryGameRoomAttribute:
# SLUS_206.42 cpnGetJoinUserLobby/cpnGetJoinUserRoom load 0x55534552 ("USER").
USER_ATTRIBUTE_TOKEN = b'USER'


def resolve_session(
    context: HandlerContext,
    message: SnapMessage,
    logger: logging.Logger,
) -> Session | None:
    """Find session by id first, then by endpoint."""

    session = context.sessions.get(message.session_id)
    if session is not None:
        return session
    session = context.sessions.get_by_endpoint(message.endpoint)
    if session is not None:
        return session
    logger.warning(
        (
            'Rejecting command 0x%02x from %s:%d: no session matched '
            '(type=0x%04x sess=0x%08x seq=%d ack=%d).'
        ),
        message.command,
        message.endpoint.host,
        message.endpoint.port,
        message.type_flags,
        message.session_id,
        message.sequence_number,
        message.acknowledge_number,
    )
    return None


def pack_fixed(value: str, size: int) -> bytes:
    """Pack text into fixed-size null-padded bytes."""

    encoded = value.encode('utf-8', errors='ignore')[:size]
    return struct.pack(f'{size}s', encoded)


def ack_for_session(session: Session) -> int:
    """ACK value for unsolicited packets sent to one client session."""

    if session.last_incoming_sequence < 0:
        return 0
    return session.last_incoming_sequence


def ack_for_request(request: SnapMessage, session: Session) -> int:
    """ACK value for request/response packets.

    Embedded commands in observed multi-send packets can carry sequence 0 while the
    enclosing reliable packet has a non-zero sequence. In that case, respond using
    the latest accepted inbound sequence for the session.
    """

    if request.sequence_number != 0:
        return request.sequence_number
    if session.last_incoming_sequence >= 0:
        return session.last_incoming_sequence
    return 0


def build_send_target_payload(payload: bytes) -> bytes | None:
    """Build relay payload for send-target callbacks.

    Binary/capture parity: server relays sender payload and only zeroes the
    target-session field at `+0x04` before forwarding to the destination client.
    """

    if len(payload) < 10:
        return None
    return payload[:4] + b'\x00\x00\x00\x00' + payload[8:]


def build_room_leave_callbacks(
    *,
    context: HandlerContext,
    leaving_session_id: int,
    recipients: list[Session],
) -> list[SnapMessage]:
    """Notify remaining room members that one peer left the room."""

    payload = struct.pack('>L', leaving_session_id)
    messages: list[SnapMessage] = []
    for member in recipients:
        if member.session_id == leaving_session_id:
            continue
        messages.append(
            context.direct(
                endpoint=member.endpoint,
                session_id=member.session_id,
                type_flags=FLAG_ROOM | FLAG_RESPONSE,
                command=commands.CMD_LEAVE,
                payload=payload,
                acknowledge_number=ack_for_session(member),
            )
        )
    return messages
