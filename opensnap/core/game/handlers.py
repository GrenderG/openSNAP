"""Game-side login and session handlers."""

from dataclasses import dataclass
import logging
import socket
import struct

from opensnap.core.context import HandlerContext
from opensnap.protocol import commands
from opensnap.protocol.constants import FLAG_CHANNEL_BITS
from opensnap.protocol.fields import get_c_string
from opensnap.protocol.models import SnapMessage

LOGGER = logging.getLogger('opensnap.core.game')


def handle_login_to_kics(context: HandlerContext, message: SnapMessage) -> list[SnapMessage]:
    """Handle `kkLoginToKICS` on the game endpoint."""

    session = context.sessions.get(message.session_id)
    if session is None:
        session = context.sessions.get_by_endpoint(message.endpoint)
    if session is None:
        LOGGER.warning(
            'Ignoring login-to-kics from %s:%d: no authenticated session is bound to this endpoint.',
            message.endpoint.host,
            message.endpoint.port,
        )
        return []

    if session.game_plugin and session.game_plugin != context.config.server.game_identifier:
        LOGGER.warning(
            (
                'Ignoring login-to-kics from %s:%d: session 0x%08x targets game %r, '
                'but this server instance is configured for %r.'
            ),
            message.endpoint.host,
            message.endpoint.port,
            session.session_id,
            session.game_plugin,
            context.config.server.game_identifier,
        )
        return []

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
    payload = struct.pack('>3L', context.config.server.game.port, 0x01234567, session.session_id)
    return [
        context.reply(
            message,
            type_flags=FLAG_CHANNEL_BITS,
            command=commands.CMD_RESULT_LOGIN_TO_KICS,
            payload=payload,
            session_id=session.session_id,
        )
    ]


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
