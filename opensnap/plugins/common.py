"""Game-agnostic SNAP helpers shared by game plugins."""

from collections.abc import Callable
import logging
import struct

from opensnap.core.context import HandlerContext
from opensnap.core.sessions import Session
from opensnap.protocol import commands
from opensnap.protocol.constants import (
    FLAG_RELIABLE,
    FLAG_RESPONSE,
    FLAG_ROOM,
    RELAY_CONTEXT_MASK,
)
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


# Reply size. Every SN@P client reads one datagram into a buffer of at most
# 896 bytes: MH NA `slti 897` at `0x001ff4f8` (EU `0x001f5588`, Outbreak F1 v2
# `0x001dd568`, F2 NA `0x00162b00`), AM `recvfrom(.., 0x380)` (`0x002ef060`,
# Beta1 `0x002e899c`). A longer one is truncated, fails the footer check and
# is dropped, so its ACK is never seen and the request is resent forever.
# Minus the 16-byte header and the 4-byte footer, a reply payload holds 876.
MAX_REPLY_PAYLOAD = 876
# A longer list goes out as pages: a page clears the room flag and carries a
# packet number; the last page is the usual reply. The dispatch (MH `0x201d68`,
# AM `0x2ed0b8`, F1 `0x1dfda8`, F2 `0x165340`) picks the callback by command,
# reads `0x2000` only for info `+1` (with the packet number in info `+2`; a list
# callback seeing both keeps waiting), feeds `0x4000` only to `kkSetRevAck`,
# and uses `0x1000` only to tell the room from the lobby QUERY_USER, so pages
# keep the lobby bit and the response flag. The list callbacks run a per-page
# count over header `+8` and append to an index reset by the request (MH room
# roster `0x006182f0`, Outbreak F1 `0x00588120`, F2 `0x005a08a0`).
MORE_PAGES_PACKET_NUMBER = 1


def build_list_pages(
    context: HandlerContext,
    message: SnapMessage,
    *,
    type_flags: int,
    command: int,
    header: Callable[[int], bytes],
    records: list[bytes],
) -> list[SnapMessage]:
    """Answer a list request in replies that each fit the client's receive buffer.

    `type_flags` are the usual reply flags and `header(count)` builds a page's
    fixed-length header for its `count` records. Every page but the last
    clears the room flag and carries a packet number; the last is the usual
    reply, so a list that fits is one reply exactly as before.
    """

    per_page = max(1, (MAX_REPLY_PAYLOAD - len(header(0))) // len(records[0])) if records else 1
    pages = [records[index:index + per_page] for index in range(0, len(records), per_page)] or [[]]
    last = len(pages) - 1
    return [
        context.reply(
            message,
            type_flags=type_flags if number == last else type_flags & ~FLAG_ROOM,
            command=command,
            payload=header(len(page)) + b''.join(page),
            packet_number=None if number == last else MORE_PAGES_PACKET_NUMBER,
        )
        for number, page in enumerate(pages)
    ]


def build_send_target_payload(payload: bytes) -> bytes | None:
    """Build relay payload for send-target callbacks.

    Binary/capture parity: server relays sender payload and only zeroes the
    target-session field at `+0x04` before forwarding to the destination client.
    """

    if len(payload) < 10:
        return None
    return payload[:4] + b'\x00\x00\x00\x00' + payload[8:]


def send_target_relay_flags(type_flags: int) -> int:
    """Type flags of a `CMD_SEND_TARGET` relay: reliable, with the sender's channel and chat bits.

    Every SDK build picks the receiving slot from those bits alone (AM
    `0x002eda28`, Beta1 `kkDispatchingOperation+0x318`, MH NA `0x00202838`,
    Outbreak = MH - `0x21fc0`): with `0x1000` only a chat target (`0x0400`)
    is delivered (slot 14), otherwise `0x0400` -> slot 12, `0x8000` -> slot 19
    (game packet), else slot 21. Game packets (`0xa000`) keep reaching slot 19.
    """

    return FLAG_RELIABLE | (type_flags & RELAY_CONTEXT_MASK)


def build_room_leave_callbacks(
    *,
    context: HandlerContext,
    leaving_session_id: int,
    recipients: list[Session],
    reliable: bool = False,
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
                type_flags=FLAG_ROOM | FLAG_RESPONSE | (FLAG_RELIABLE if reliable else 0),
                command=commands.CMD_LEAVE,
                payload=payload,
                acknowledge_number=ack_for_session(member),
            )
        )
    return messages

