"""Game-side login and session handlers."""

from dataclasses import dataclass
import logging
import socket
import struct

from opensnap.core.context import HandlerContext
from opensnap.protocol import commands
from opensnap.protocol.constants import FLAG_CHANNEL_BITS, FLAG_RESPONSE, FLAG_ROOM
from opensnap.protocol.fields import get_c_string
from opensnap.protocol.models import SnapMessage

LOGGER = logging.getLogger('opensnap.core.game')


def handle_send_echo(context: HandlerContext, message: SnapMessage) -> list[SnapMessage]:
    """Respond to keepalive/echo packets by mirroring full payload bytes.

    `SLUS_206.42` `kkSendEchoPacket` copies the caller-provided payload
    length verbatim (observed 8-byte and 64-byte calls), and the echo
    callback variants only clear local state flags.
    """

    payload = message.payload
    channel = message.type_flags & FLAG_CHANNEL_BITS
    if channel == 0:
        channel = FLAG_ROOM

    return [
        context.reply(
            message,
            type_flags=channel | FLAG_RESPONSE,
            command=message.command,
            payload=payload,
        )
    ]


def handle_login_to_kics(context: HandlerContext, message: SnapMessage) -> list[SnapMessage]:
    """Handle `kkLoginToKICS` on the game endpoint."""

    handoff = context.handoffs.get(message.session_id) or context.handoffs.get_by_endpoint(message.endpoint)
    if handoff is None:
        LOGGER.warning(
            'Ignoring login-to-kics from %s:%d: no authenticated session is bound to this endpoint.',
            message.endpoint.host,
            message.endpoint.port,
        )
        return []

    if handoff.game_identifier and handoff.game_identifier != context.config.server.game_plugin:
        LOGGER.warning(
            (
                'Ignoring login-to-kics from %s:%d: session 0x%08x targets game %r, '
                'but this server instance is configured for %r.'
            ),
            message.endpoint.host,
            message.endpoint.port,
            handoff.session_id,
            handoff.game_identifier,
            context.config.server.game_plugin,
        )
        return []

    # A console that crashed and logs in again reuses its endpoint (the PS2
    # always sends from the same port) under a new session; end the old one
    # properly, as a timeout would, so it does not linger in rooms and lists.
    outbound: list[SnapMessage] = []
    stale = context.sessions.get_by_endpoint(message.endpoint)
    if stale is not None and stale.session_id != handoff.session_id:
        LOGGER.info(
            'Ending session 0x%08x of %s:%d: superseded by the new login of session 0x%08x.',
            stale.session_id,
            message.endpoint.host,
            message.endpoint.port,
            handoff.session_id,
        )
        outbound = context.end_session(stale)

    session = context.sessions.get(handoff.session_id)
    if session is not None:
        _reset_room_state_for_relogin(context, session)
    # A repeated KICS of the same login keeps its counters; a new bootstrap
    # login starts a runtime session continuing the bootstrap's numbering.
    if session is None or session.handoff_serial != handoff.serial:
        session = context.sessions.adopt(handoff)

    if session.endpoint != message.endpoint:
        rebound = context.sessions.rebind_endpoint(session.session_id, message.endpoint)
        if rebound is not None:
            session = rebound

    if len(message.payload) < 0x130:
        LOGGER.warning(
            (
                'Received short login-to-kics payload from %s:%d '
                '(len=%d, expected>=304); parsing available fields only.'
            ),
            message.endpoint.host,
            message.endpoint.port,
            len(message.payload),
        )

    parsed = parse_kics_login_payload(message.payload)
    context.accounts.set_team(session.user_id, parsed.team)
    # `CMD_RESULT_LOGIN_TO_KICS` words, identical in `SLUS_206.42`
    # (`kkDispatchingOperation` `0x002ede78..0x002ee004`) and `SLUS_208.96`
    # (`0x00202cf8..0x00202e0c`):
    # - word0: low 16 bits become the connection's server port (host kept), so
    #   returning this server's own port keeps the client on this socket;
    # - word1: overwritten with `1` before the callback, never read;
    # - word2: stored as the client's connection session id (`ctx + 0x44`).
    payload = struct.pack('>3L', context.config.server.game.port, 0x01234567, session.session_id)
    return [
        context.reply(
            message,
            type_flags=FLAG_CHANNEL_BITS,
            command=commands.CMD_RESULT_LOGIN_TO_KICS,
            payload=payload,
            session_id=session.session_id,
        )
    ] + outbound


def _reset_room_state_for_relogin(context: HandlerContext, session) -> None:
    """Silently clear stale room membership when a client re-enters via KICS login."""

    room_id = session.room_id
    if room_id <= 0:
        return

    room = context.rooms.get(room_id)
    if room is None:
        context.sessions.set_room(session.session_id, 0)
        return

    if session.session_id == room.host_session_id:
        for member in context.sessions.list_room_members(room_id):
            context.sessions.set_room(member.session_id, 0)
        for member_session_id in tuple(room.members):
            context.rooms.leave(room_id, member_session_id)
        return

    context.rooms.leave(room_id, session.session_id)
    context.sessions.set_room(session.session_id, 0)


@dataclass(frozen=True, slots=True)
class KicsLoginPayload:
    """Structured view of known `kkLoginToKICS` payload fields."""

    client_ip: str
    mtu_hint: int
    client_flags: int
    version_code: int
    login: str
    region_code: int
    marker_bb: int
    marker_dd: int
    auth_blob: bytes
    team: str


def parse_kics_login_payload(payload: bytes) -> KicsLoginPayload:
    """Parse the confirmed `kkLoginToKICS` payload offsets from captures."""

    return KicsLoginPayload(
        client_ip=_read_ipv4(payload, 0),
        mtu_hint=_read_u32(payload, 4),
        client_flags=_read_u32(payload, 8),
        version_code=_read_u32(payload, 12),
        login=get_c_string(payload, 16).rstrip('\n'),
        region_code=_read_u32(payload, 0x20),
        marker_bb=_read_u32(payload, 0x24),
        marker_dd=_read_u32(payload, 0x28),
        auth_blob=_slice(payload, 0x80, 0x80),
        team=get_c_string(payload, 0x128),
    )


def _read_u32(payload: bytes, offset: int) -> int:
    """Read one big-endian uint32, or zero when truncated."""

    if offset + 4 > len(payload):
        return 0
    return struct.unpack_from('>L', payload, offset)[0]


def _read_ipv4(payload: bytes, offset: int) -> str:
    """Read one IPv4 field, or fall back to loopback when invalid."""

    raw_ip = _read_u32(payload, offset)
    try:
        return socket.inet_ntoa(struct.pack('>L', raw_ip))
    except OSError:
        return '127.0.0.1'


def _slice(payload: bytes, offset: int, length: int) -> bytes:
    """Return one bounded payload slice."""

    if offset >= len(payload):
        return b''
    end = min(len(payload), offset + length)
    return payload[offset:end]
