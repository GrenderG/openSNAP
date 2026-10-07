"""Game-agnostic SNAP helpers shared by game plugins."""

from dataclasses import replace
import logging
import struct

from opensnap.core.context import HandlerContext
from opensnap.core.sessions import Session
from opensnap.protocol import commands
from opensnap.protocol.constants import FLAG_CHANNEL_BITS, FLAG_RELIABLE, FLAG_RESPONSE, FLAG_ROOM
from opensnap.protocol.models import SnapMessage


# Binary-verified attribute selector used by kkQueryLobbyAttribute/kkQueryGameRoomAttribute:
# SLUS_206.42 cpnGetJoinUserLobby/cpnGetJoinUserRoom load 0x55534552 ("USER").
USER_ATTRIBUTE_TOKEN = b'USER'

# Attribute searches of the Dec 2003 SDK (`CMD_QUERY_AREA`, `CMD_SEARCH_ROOMS`;
# `SLUS_208.96` `0x0020873c`/`0x00208cc8`, `SLUS_207.65` `0x001e677c`/
# `0x001e6d08`): `u32 limit, u8 count`, padded to 8 bytes, then conditions
# `u32 name, u8 type << 5 | operator, value` with 4/8/16-byte values for
# types 0/1/2. Observed operators: 1 equal (`OID`; MH `0x00613a8c`, Outbreak
# `0x0058adac`), 4 at least and 6 at most (`NAME`; MH `0x00612950`).
SEARCH_HEADER_SIZE = 8
SEARCH_VALUE_SIZES = {0: 4, 1: 8, 2: 16}
SEARCH_EQUAL = 1
SEARCH_AT_LEAST = 4
SEARCH_AT_MOST = 6
SEARCH_OID = b'OID\x00'
SEARCH_NAME = b'NAME'


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


def parse_attribute_search(payload: bytes) -> tuple[int, list[tuple[bytes, int, bytes]]]:
    """Parse one attribute search request into `(limit, [(name, operator, value)])`."""

    if len(payload) < SEARCH_HEADER_SIZE:
        return 0, []
    limit, count = struct.unpack_from('>L', payload, 0)[0], payload[4]
    conditions = []
    offset = SEARCH_HEADER_SIZE
    for _ in range(count):
        if offset + 5 > len(payload):
            break
        name, flags = payload[offset:offset + 4], payload[offset + 4]
        size = SEARCH_VALUE_SIZES.get(flags >> 5)
        if size is None or offset + 5 + size > len(payload):
            break
        conditions.append((name, flags & 0x1F, payload[offset + 5:offset + 5 + size]))
        offset += 5 + size
    return limit, conditions


def search_text(value: bytes) -> str:
    """Text of one search value (NUL-terminated ASCII)."""

    return value.split(b'\x00', 1)[0].decode('ascii', errors='ignore')


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


class ReliableReplyCache:
    """Replay a requester's original result for an exact reliable retry.

    The SN@P client retransmits a reliable request until it is ACKed and
    applies a second copy of any callback (MH room join `0x00619430` counts the
    member twice), so a retried room change must not run again. Results are
    kept per `(session, command, channel)` with the request sequence; only the
    requester's own messages are replayed, re-addressed to its endpoint.
    """

    def __init__(self) -> None:
        self._replies: dict[tuple[int, int, int], tuple[int, tuple[SnapMessage, ...]]] = {}

    def replay(self, message: SnapMessage, session: Session) -> list[SnapMessage] | None:
        key = _reliable_reply_key(message, session)
        cached = None if key is None else self._replies.get(key)
        if cached is None or cached[0] != message.sequence_number:
            return None
        return [
            replace(cached_message, endpoint=message.endpoint)
            if cached_message.session_id == session.session_id
            else replace(cached_message)
            for cached_message in cached[1]
        ]

    def remember(self, message: SnapMessage, session: Session, outbound: list[SnapMessage]) -> None:
        key = _reliable_reply_key(message, session)
        if key is not None:
            self._replies[key] = (
                message.sequence_number,
                tuple(replace(item) for item in outbound if item.session_id == session.session_id),
            )

    def forget(self, session_id: int) -> None:
        for key in [key for key in self._replies if key[0] == session_id]:
            self._replies.pop(key, None)


def _reliable_reply_key(message: SnapMessage, session: Session) -> tuple[int, int, int] | None:
    if (message.type_flags & FLAG_RELIABLE) == 0 or message.embedded_in_multi:
        return None
    return (session.session_id, message.command, message.type_flags & FLAG_CHANNEL_BITS)
