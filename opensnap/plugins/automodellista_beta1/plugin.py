"""Auto Modellista beta1 game plugin."""

import logging
import struct

from opensnap.core.context import HandlerContext
from opensnap.plugins.automodellista import AutoModellistaPlugin
from opensnap.plugins.automodellista.plugin import USER_ATTRIBUTE_TOKEN, _ack_for_session, _prune_stale_room_members
from opensnap.protocol import commands
from opensnap.protocol.codec import PacketDecodeError, decode_datagram as decode_snap_datagram, encode_messages as encode_snap_messages
from opensnap.protocol.constants import FLAG_CHANNEL_BITS, FLAG_RESPONSE, FLAG_ROOM
from opensnap.protocol.fields import get_len_prefixed_string, get_u16, get_u32
from opensnap.protocol.models import Endpoint, SnapMessage, WIRE_FORMAT_AM_BETA1_LEGACY, WIRE_FORMAT_SNAP

_LOGGER = logging.getLogger('opensnap.plugins.automodellista_beta1')

LEGACY_ROOM_ENTRY_COMMAND = 0x6406
LEGACY_ROOM_CURRENT_PLAYERS_COMMAND = 0x6403
LEGACY_ROOM_INFO_COMMAND = 0x640B
_LEGACY_HEADER_SIZE = 16
_LEGACY_MARKER = 0x81
_LEGACY_TRAILER = b'\xff\xff\xff'


class AutoModellistaBeta1Plugin(AutoModellistaPlugin):
    """Auto Modellista beta1 behavior layered on top of shared SNAP handlers."""

    name = 'automodellista_beta1'

    def register_handlers(self, router, context: HandlerContext) -> None:
        super().register_handlers(router, context)
        router.register(LEGACY_ROOM_ENTRY_COMMAND, self._handle_legacy_room_entry)

    def _handle_join(self, context: HandlerContext, message: SnapMessage) -> list[SnapMessage]:
        room_id = _room_id_from_join_message(message)
        result = super()._handle_join(context, message)

        if room_id == 0 or not _produced_peer_membership_callback(result, commands.CMD_JOIN, message.endpoint):
            return result

        return result + _build_room_user_count_callbacks(
            context=context,
            room_id=room_id,
            excluding_endpoint=message.endpoint,
        )

    def _handle_leave(self, context: HandlerContext, message: SnapMessage) -> list[SnapMessage]:
        room_id = _room_id_before_leave(context, message)
        result = super()._handle_leave(context, message)

        if room_id == 0 or not _produced_peer_membership_callback(result, commands.CMD_LEAVE, message.endpoint):
            return result

        return result + _build_room_user_count_callbacks(
            context=context,
            room_id=room_id,
            excluding_endpoint=message.endpoint,
        )

    def decode_datagram(self, payload: bytes, endpoint: Endpoint) -> list[SnapMessage]:
        """Decode SNAP datagrams plus the beta1 legacy room-entry packet family."""

        try:
            return decode_snap_datagram(payload, endpoint)
        except PacketDecodeError as snap_exc:
            legacy_message = _decode_legacy_datagram(payload, endpoint)
            if legacy_message is None:
                raise snap_exc
            return [legacy_message]

    def encode_messages(self, messages: list[SnapMessage], *, footer_bytes: bytes | None = None) -> bytes:
        """Encode outbound beta1 messages using the correct wire format."""

        if not messages:
            raise ValueError('At least one message is required.')
        if messages[0].wire_format == WIRE_FORMAT_SNAP:
            return encode_snap_messages(messages, footer_bytes=footer_bytes)
        if len(messages) != 1:
            raise ValueError('Auto Modellista beta1 legacy datagrams must contain exactly one message.')
        return _encode_legacy_datagram(messages[0])

    def _handle_legacy_room_entry(self, context: HandlerContext, message: SnapMessage) -> list[SnapMessage]:
        try:
            room_id = get_u16(message.payload, 0)
            get_len_prefixed_string(message.payload, 2)
        except struct.error:
            _LOGGER.warning(
                'Rejecting Auto Modellista beta1 legacy room-entry from %s:%d: payload is truncated.',
                message.endpoint.host,
                message.endpoint.port,
            )
            return []

        session = context.sessions.get_by_endpoint(message.endpoint)
        if session is None:
            _LOGGER.warning(
                (
                    'Rejecting Auto Modellista beta1 legacy room-entry command '
                    f'for room {room_id}: no session matched {message.endpoint.host}:{message.endpoint.port}.'
                ),
            )
            return []

        _prune_stale_room_members(context, room_id)
        room = context.rooms.get(room_id)
        if room is None:
            _LOGGER.warning(
                'Ignoring Auto Modellista beta1 legacy room-entry for unknown room %d from %s:%d.',
                room_id,
                message.endpoint.host,
                message.endpoint.port,
            )
            return []

        current_players = len(room.members)
        max_players = min(room.max_players, context.config.server.max_players_per_room)
        return [
            SnapMessage(
                endpoint=message.endpoint,
                type_flags=message.type_flags,
                packet_number=0,
                command=LEGACY_ROOM_CURRENT_PLAYERS_COMMAND,
                session_id=0,
                sequence_number=0,
                acknowledge_number=0,
                payload=struct.pack('>2H', room_id, current_players),
                wire_format=WIRE_FORMAT_AM_BETA1_LEGACY,
            ),
            SnapMessage(
                endpoint=message.endpoint,
                type_flags=message.type_flags,
                packet_number=0,
                command=LEGACY_ROOM_INFO_COMMAND,
                session_id=0,
                sequence_number=0,
                acknowledge_number=0,
                payload=struct.pack('>6H', room_id, max_players, 0, 0, 0, 0),
                wire_format=WIRE_FORMAT_AM_BETA1_LEGACY,
            ),
        ]

def _decode_legacy_datagram(payload: bytes, endpoint: Endpoint) -> SnapMessage | None:
    """Decode one beta1 legacy room-entry datagram."""

    if len(payload) < _LEGACY_HEADER_SIZE:
        return None
    if payload[4] != _LEGACY_MARKER or payload[13:16] != _LEGACY_TRAILER:
        return None

    payload_length = struct.unpack_from('>H', payload, 8)[0]
    total_length = _LEGACY_HEADER_SIZE + payload_length
    if len(payload) != total_length:
        raise PacketDecodeError(
            (
                'Auto Modellista beta1 legacy datagram length mismatch: '
                f'header says {payload_length} body byte(s), got {len(payload) - _LEGACY_HEADER_SIZE}.'
            )
        )

    return SnapMessage(
        endpoint=endpoint,
        type_flags=payload[5],
        packet_number=0,
        command=struct.unpack_from('>H', payload, 6)[0],
        session_id=0,
        sequence_number=0,
        acknowledge_number=0,
        payload=payload[_LEGACY_HEADER_SIZE:total_length],
        wire_format=WIRE_FORMAT_AM_BETA1_LEGACY,
    )


def _encode_legacy_datagram(message: SnapMessage) -> bytes:
    """Encode one beta1 legacy room-entry datagram."""

    if message.wire_format != WIRE_FORMAT_AM_BETA1_LEGACY:
        raise ValueError(f'Unsupported Auto Modellista beta1 wire format: {message.wire_format}.')

    header = bytearray(_LEGACY_HEADER_SIZE)
    header[4] = _LEGACY_MARKER
    header[5] = message.type_flags & 0xFF
    struct.pack_into('>H', header, 6, message.command & 0xFFFF)
    struct.pack_into('>H', header, 8, len(message.payload))
    header[13:16] = _LEGACY_TRAILER
    return bytes(header) + message.payload


def _room_id_from_join_message(message: SnapMessage) -> int:
    """Return the requested room id for a normal beta1 room join."""

    if (message.type_flags & FLAG_CHANNEL_BITS) != FLAG_ROOM:
        return 0
    if len(message.payload) < 4:
        return 0
    return get_u32(message.payload, 0)


def _room_id_before_leave(context: HandlerContext, message: SnapMessage) -> int:
    """Return the caller's current room before the shared leave handler clears it."""

    session = context.sessions.get(message.session_id)
    if session is None:
        return 0
    if (message.type_flags & FLAG_CHANNEL_BITS) != FLAG_ROOM:
        return 0
    return session.room_id


def _produced_peer_membership_callback(
    messages: list[SnapMessage],
    command: int,
    requester: Endpoint,
) -> bool:
    """Detect one real membership change callback, not a wrapper-only retry."""

    return any(message.command == command and message.endpoint != requester for message in messages)


def _build_room_user_count_callbacks(
    *,
    context: HandlerContext,
    room_id: int,
    excluding_endpoint: Endpoint,
) -> list[SnapMessage]:
    """Push the beta1 room USER count callback to existing room members."""

    room = context.rooms.get(room_id)
    if room is None:
        return []

    payload = struct.pack('>L4sL', room_id, USER_ATTRIBUTE_TOKEN, len(room.members))
    messages: list[SnapMessage] = []
    for member in context.sessions.list_room_members(room_id):
        if member.endpoint == excluding_endpoint:
            continue
        messages.append(
            context.direct(
                endpoint=member.endpoint,
                session_id=member.session_id,
                type_flags=FLAG_ROOM | FLAG_RESPONSE,
                command=commands.CMD_QUERY_ATTRIBUTE,
                payload=payload,
                acknowledge_number=_ack_for_session(member),
            )
        )
    return messages
