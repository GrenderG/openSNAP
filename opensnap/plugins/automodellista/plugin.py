"""Auto Modellista game plugin."""

import logging
import struct

from opensnap.core.context import HandlerContext
from opensnap.core.router import CommandRouter
from opensnap.core.sessions import Session
from opensnap.protocol import commands
from opensnap.protocol.constants import CHANNEL_LOBBY, CHANNEL_ROOM, FLAG_MULTI, FLAG_RELIABLE, FLAG_RESPONSE
from opensnap.protocol.fields import get_c_string, get_u16, get_u32
from opensnap.protocol.models import SnapMessage

MAX_ROOMS_PER_LOBBY = 50
_LOGGER = logging.getLogger('opensnap.plugins.automodellista')
# Binary-verified attribute selector used by kkQueryLobbyAttribute/kkQueryGameRoomAttribute:
# SLUS_204.98 cpnGetJoinUserLobby/cpnGetJoinUserRoom load 0x55534552 ("USER").
USER_ATTRIBUTE_TOKEN = b'USER'
# Legacy operation identifiers expected in CMD_RESULT_WRAPPER (0x28) payload
# word0 for Auto Modellista client progression.
CB_CREATE_GAME_ROOM = 0x04
CB_JOIN_LOBBY = 0x06
CB_JOIN_GAME_ROOM = 0x06
CB_LEAVE_LOBBY = 0x07
CB_LEAVE_GAME_ROOM = 0x07
CB_CHANGE_ATTRIBUTE = 0x08
CB_CHANGE_USER_PROPERTY = 0x0C
CB_CHANGE_USER_STATUS = 0x0D


class AutoModellistaPlugin:
    """Command handlers for Auto Modellista behavior."""

    name = 'automodellista'

    def __init__(self) -> None:
        # Retransmitted reliable create-room packets reuse sequence numbers.
        self._create_room_results: dict[tuple[int, int], int] = {}

    def register_handlers(self, router: CommandRouter, context: HandlerContext) -> None:
        """Register plugin handlers."""

        del context
        router.register(commands.CMD_QUERY_LOBBIES, self._handle_query_lobbies)
        router.register(commands.CMD_QUERY_ATTRIBUTE, self._handle_query_attribute)
        router.register(commands.CMD_QUERY_GAME_ROOMS, self._handle_query_game_rooms)
        router.register(commands.CMD_QUERY_USER, self._handle_query_user)
        router.register(commands.CMD_CREATE_GAME_ROOM, self._handle_create_game_room)
        router.register(commands.CMD_JOIN, self._handle_join)
        router.register(commands.CMD_LEAVE, self._handle_leave)
        router.register(commands.CMD_SEND, self._handle_send)
        router.register(commands.CMD_SEND_TARGET, self._handle_send_target)
        router.register(commands.CMD_CHANGE_USER_STATUS, self._handle_change_user_status)
        router.register(commands.CMD_CHANGE_USER_PROPERTY, self._handle_change_user_property)
        router.register(commands.CMD_CHANGE_ATTRIBUTE, self._handle_change_attribute)

    def on_tick(self, context: HandlerContext) -> list[SnapMessage]:
        """No periodic game-specific packets yet."""

        del context
        return []

    def _handle_query_lobbies(self, context: HandlerContext, message: SnapMessage) -> list[SnapMessage]:
        entries = []
        for lobby in context.lobbies.list():
            users_in = context.sessions.count_users_in_lobby(lobby.lobby_id)
            entries.append(struct.pack('>16s3L', _pack_fixed(lobby.name, 16), users_in, 0, lobby.lobby_id))

        payload = struct.pack('>3L', 0, 1, len(entries)) + b''.join(entries)
        return [
            context.reply(
                message,
                type_flags=CHANNEL_LOBBY | FLAG_RESPONSE,
                command=commands.CMD_QUERY_LOBBIES,
                payload=payload,
            )
        ]

    def _handle_query_attribute(self, context: HandlerContext, message: SnapMessage) -> list[SnapMessage]:
        if message.type_flags & FLAG_MULTI:
            # Keep behavior where one multi-packet contains lobby USER counts.
            payload = self._build_multi_lobby_user_query_payload(context, message)
            type_flags = CHANNEL_LOBBY | FLAG_RESPONSE | FLAG_MULTI
            size_word_override = type_flags | 0x001C
            return [
                context.reply(
                    message,
                    type_flags=type_flags,
                    command=commands.CMD_QUERY_ATTRIBUTE,
                    payload=payload,
                    size_word_override=size_word_override,
                )
            ]

        lobby_id = get_u32(message.payload, 0)
        payload = struct.pack(
            '>L4sL',
            lobby_id,
            USER_ATTRIBUTE_TOKEN,
            context.sessions.count_users_in_lobby(lobby_id),
        )
        return [
            context.reply(
                message,
                type_flags=CHANNEL_LOBBY | FLAG_RESPONSE,
                command=commands.CMD_QUERY_ATTRIBUTE,
                payload=payload,
            )
        ]

    def _handle_query_game_rooms(self, context: HandlerContext, message: SnapMessage) -> list[SnapMessage]:
        lobby_id = get_u32(message.payload, 0)
        _prune_stale_rooms_in_lobby(context, lobby_id)
        rooms = context.rooms.list_for_lobby(lobby_id)
        entries = []
        for room in rooms:
            entries.append(
                struct.pack(
                    '>16s5L',
                    _pack_fixed(room.name, 16),
                    len(room.members),
                    0,
                    room.rules,
                    room.max_players,
                    room.room_id,
                )
            )

        payload = struct.pack('>3L', 0, 1, len(entries)) + b''.join(entries)
        return [
            context.reply(
                message,
                type_flags=CHANNEL_LOBBY | FLAG_RESPONSE,
                command=commands.CMD_QUERY_GAME_ROOMS,
                payload=payload,
            )
        ]

    def _handle_query_user(self, context: HandlerContext, message: SnapMessage) -> list[SnapMessage]:
        if (message.type_flags & 0x3000) != CHANNEL_ROOM:
            return []

        room_id = get_u32(message.payload, 0)
        members = context.sessions.list_room_members(room_id)
        entries = []
        for session in members:
            account = context.accounts.get_by_id(session.user_id)
            team = _network_team('' if account is None else account.team)
            entries.append(
                struct.pack(
                    '>16s2L32s',
                    _pack_fixed(_network_username(session.username), 16),
                    session.session_id,
                    32,
                    _pack_fixed(team, 32),
                )
            )

        payload = struct.pack('>3L', room_id, len(entries), len(entries)) + b''.join(entries)
        return [
            context.reply(
                message,
                type_flags=CHANNEL_ROOM | FLAG_RESPONSE,
                command=commands.CMD_QUERY_USER,
                payload=payload,
            )
        ]

    def _handle_create_game_room(self, context: HandlerContext, message: SnapMessage) -> list[SnapMessage]:
        session = _resolve_session(context, message)
        if session is None:
            return []

        cache_key = (session.session_id, message.sequence_number)
        cached_result = self._create_room_results.get(cache_key)
        if cached_result is not None:
            return [
                context.reply(
                    message,
                    type_flags=CHANNEL_ROOM | FLAG_RESPONSE,
                    command=commands.CMD_RESULT_WRAPPER,
                    payload=struct.pack('>2L', CB_CREATE_GAME_ROOM, cached_result),
                    session_id=session.session_id,
                )
            ]

        _prune_stale_rooms_in_lobby(context, session.lobby_id)
        room_name = get_c_string(message.payload, 0)
        room_password = get_c_string(message.payload, 0x14)
        max_players = max(get_u32(message.payload, 0x10), 1)
        rules = get_u32(message.payload, 0x28)

        room_count = len(context.rooms.list_for_lobby(session.lobby_id))
        if room_count >= MAX_ROOMS_PER_LOBBY:
            self._create_room_results[cache_key] = 1
            return [
                context.reply(
                    message,
                    type_flags=CHANNEL_ROOM | FLAG_RESPONSE,
                    command=commands.CMD_RESULT_WRAPPER,
                    payload=struct.pack('>2L', CB_CREATE_GAME_ROOM, 1),
                    session_id=session.session_id,
                )
            ]

        room = context.rooms.create_room(
            name=room_name,
            password=room_password,
            rules=rules,
            max_players=max_players,
            lobby_id=session.lobby_id,
            host_session_id=session.session_id,
        )
        context.sessions.set_room(session.session_id, room.room_id)
        self._create_room_results[cache_key] = room.room_id

        payload = struct.pack('>2L', CB_CREATE_GAME_ROOM, room.room_id)
        return [
            context.reply(
                message,
                type_flags=CHANNEL_ROOM | FLAG_RESPONSE,
                command=commands.CMD_RESULT_WRAPPER,
                payload=payload,
                session_id=session.session_id,
            )
        ]

    def _handle_join(self, context: HandlerContext, message: SnapMessage) -> list[SnapMessage]:
        session = _resolve_session(context, message)
        if session is None:
            return []

        if (message.type_flags & 0x3000) == CHANNEL_LOBBY:
            if session.room_id > 0:
                context.rooms.leave(session.room_id, session.session_id)
                context.sessions.set_room(session.session_id, 0)
            lobby_id = get_u32(message.payload, 0)
            context.sessions.set_lobby(session.session_id, lobby_id)
            context.sessions.set_room(session.session_id, 0)
            payload = struct.pack('>2L', CB_JOIN_LOBBY, 0)
            return [
                context.reply(
                    message,
                    type_flags=CHANNEL_LOBBY | FLAG_RESPONSE,
                    command=commands.CMD_RESULT_WRAPPER,
                    payload=payload,
                    session_id=session.session_id,
                )
            ]

        if (message.type_flags & 0x3000) == CHANNEL_ROOM:
            room_id = get_u32(message.payload, 0)
            _prune_stale_room_members(context, room_id)
            room = context.rooms.get(room_id)

            # Reliable join retransmits are expected on packet loss.
            # If the client is already in this room, treat as idempotent success
            # and avoid replaying host/join callbacks that can desynchronize state.
            if room is not None and session.room_id == room_id and session.session_id in room.members:
                payload = struct.pack('>2L', CB_JOIN_GAME_ROOM, 0)
                return [
                    context.reply(
                        message,
                        type_flags=CHANNEL_ROOM | FLAG_RESPONSE,
                        command=commands.CMD_RESULT_WRAPPER,
                        payload=payload,
                        session_id=session.session_id,
                    )
                ]

            existing_members = [
                member for member in context.sessions.list_room_members(room_id)
                if member.session_id != session.session_id
            ]
            success = context.rooms.join(room_id, session.session_id)
            if success:
                context.sessions.set_room(session.session_id, room_id)
                callbacks = _build_room_join_callbacks(
                    context=context,
                    joining_session=session,
                    recipients=existing_members,
                )
                payload = struct.pack('>2L', CB_JOIN_GAME_ROOM, 0)
            else:
                callbacks = []
                payload = struct.pack('>2L', CB_JOIN_GAME_ROOM, 1)

            return [
                context.reply(
                    message,
                    type_flags=CHANNEL_ROOM | FLAG_RESPONSE,
                    command=commands.CMD_RESULT_WRAPPER,
                    payload=payload,
                    session_id=session.session_id,
                )
            ] + callbacks

        return []

    def _handle_leave(self, context: HandlerContext, message: SnapMessage) -> list[SnapMessage]:
        session = _resolve_session(context, message)
        if session is None:
            return []
        acknowledge_number = _ack_for_request(message, session)

        if (message.type_flags & 0x3000) == CHANNEL_LOBBY:
            callbacks: list[SnapMessage] = []
            if session.room_id > 0:
                room_id = session.room_id
                recipients = [
                    member for member in context.sessions.list_room_members(room_id)
                    if member.session_id != session.session_id
                ]
                context.rooms.leave(room_id, session.session_id)
                context.sessions.set_room(session.session_id, 0)
                callbacks = _build_room_leave_callbacks(
                    context=context,
                    leaving_session_id=session.session_id,
                    recipients=recipients,
                )
            context.sessions.set_lobby(session.session_id, 0)
            payload = struct.pack('>2L', CB_LEAVE_LOBBY, 0)
            return [
                context.reply(
                    message,
                    type_flags=CHANNEL_LOBBY | FLAG_RESPONSE,
                    command=commands.CMD_RESULT_WRAPPER,
                    payload=payload,
                    session_id=session.session_id,
                    acknowledge_number=acknowledge_number,
                )
            ] + callbacks

        if (message.type_flags & 0x3000) == CHANNEL_ROOM:
            callbacks: list[SnapMessage] = []
            room_id = session.room_id
            if room_id > 0:
                recipients = [
                    member for member in context.sessions.list_room_members(room_id)
                    if member.session_id != session.session_id
                ]
                context.rooms.leave(room_id, session.session_id)
                context.sessions.set_room(session.session_id, 0)
                callbacks = _build_room_leave_callbacks(
                    context=context,
                    leaving_session_id=session.session_id,
                    recipients=recipients,
                )
            payload = struct.pack('>2L', CB_LEAVE_GAME_ROOM, 0)
            return [
                context.reply(
                    message,
                    type_flags=CHANNEL_ROOM | FLAG_RESPONSE,
                    command=commands.CMD_RESULT_WRAPPER,
                    payload=payload,
                    session_id=session.session_id,
                    acknowledge_number=acknowledge_number,
                )
            ] + callbacks

        return []

    def _handle_send(self, context: HandlerContext, message: SnapMessage) -> list[SnapMessage]:
        session = _resolve_session(context, message)
        if session is None:
            return []

        if message.type_flags & FLAG_MULTI:
            # TODO(openSNAP): Keep validating multi-send relay against captures.
            #  Current implementation relays tunneled room payloads in multi packets
            #  using the same sender/all-members policy as single SEND packets.
            #  Re-check once additional ready-battle and in-game traces are decoded.
            channel = message.type_flags & 0x3000
            if channel == 0:
                channel = CHANNEL_ROOM
            responses = [context.reply(
                message,
                type_flags=channel | FLAG_RESPONSE,
                command=commands.CMD_ACK,
                session_id=session.session_id,
            )]

            if channel == CHANNEL_ROOM and len(message.payload) >= 2:
                include_sender = _should_include_sender_for_room_game_payload(message.payload)
                exclude_session_id = None if include_sender else session.session_id
                responses.extend(
                    _broadcast_room_game_packet(
                        context=context,
                        request=message,
                        room_id=session.room_id,
                        payload=message.payload,
                        exclude_session_id=exclude_session_id,
                        type_flags=(message.type_flags & ~FLAG_MULTI),
                    )
                )

            return responses

        if (message.type_flags & 0x3400) == 0x1400:
            ack_to_sender = context.reply(
                message,
                type_flags=CHANNEL_LOBBY | FLAG_RESPONSE,
                command=commands.CMD_ACK,
                session_id=session.session_id,
            )
            chat_payload = _build_chat_echo_payload(session, message.payload)
            chats = _broadcast_lobby_chat(
                context,
                session.lobby_id,
                chat_payload,
                exclude_session_id=session.session_id,
            )
            return [ack_to_sender] + chats

        if (message.type_flags & 0x3400) == 0x2400:
            ack_to_sender = context.reply(
                message,
                type_flags=CHANNEL_ROOM | FLAG_RESPONSE,
                command=commands.CMD_ACK,
                session_id=session.session_id,
            )
            chat_payload = _build_chat_echo_payload(session, message.payload)
            chats = _broadcast_room_chat(
                context,
                session.room_id,
                chat_payload,
                exclude_session_id=session.session_id,
            )
            return [ack_to_sender] + chats

        if message.type_flags & CHANNEL_ROOM:
            ack_to_sender = context.reply(
                message,
                type_flags=CHANNEL_ROOM | FLAG_RESPONSE,
                command=commands.CMD_ACK,
                session_id=session.session_id,
            )

            if len(message.payload) < 2:
                return [ack_to_sender]

            include_sender = _should_include_sender_for_room_game_payload(message.payload)
            exclude_session_id = None if include_sender else session.session_id
            broadcasts = _broadcast_room_game_packet(
                context=context,
                request=message,
                room_id=session.room_id,
                payload=message.payload,
                exclude_session_id=exclude_session_id,
            )
            return [ack_to_sender] + broadcasts

        return []

    def _handle_send_target(self, context: HandlerContext, message: SnapMessage) -> list[SnapMessage]:
        session = _resolve_session(context, message)
        if session is None:
            return []

        response = [
            context.reply(
                message,
                type_flags=CHANNEL_ROOM | FLAG_RESPONSE,
                command=commands.CMD_ACK,
                session_id=session.session_id,
            )
        ]

        if len(message.payload) < 10:
            _LOGGER.debug(
                'Ignoring short send-target payload from %s:%d (len=%d).',
                message.endpoint.host,
                message.endpoint.port,
                len(message.payload),
            )
            return response

        target_session_id = get_u32(message.payload, 4)
        target = context.sessions.get(target_session_id)
        if target is None:
            _LOGGER.debug(
                (
                    'Skipping send-target relay from %s:%d: '
                    'target session 0x%08x not found (sender session 0x%08x).'
                ),
                message.endpoint.host,
                message.endpoint.port,
                target_session_id,
                session.session_id,
            )
            return response

        relay_payload = _build_send_target_payload(message.payload)
        if relay_payload is None:
            _LOGGER.debug(
                'Skipping send-target relay from %s:%d: payload len=%d is too short.',
                message.endpoint.host,
                message.endpoint.port,
                len(message.payload),
            )
            return response

        response.append(
            context.direct(
                endpoint=target.endpoint,
                session_id=target.session_id,
                type_flags=CHANNEL_ROOM | FLAG_RELIABLE,
                command=commands.CMD_SEND_TARGET,
                payload=relay_payload,
                acknowledge_number=_ack_for_session(target),
            )
        )
        return response

    def _handle_change_user_status(self, context: HandlerContext, message: SnapMessage) -> list[SnapMessage]:
        return self._simple_change_ack(context, message, CB_CHANGE_USER_STATUS)

    def _handle_change_user_property(self, context: HandlerContext, message: SnapMessage) -> list[SnapMessage]:
        return self._simple_change_ack(context, message, CB_CHANGE_USER_PROPERTY)

    def _handle_change_attribute(self, context: HandlerContext, message: SnapMessage) -> list[SnapMessage]:
        return self._simple_change_ack(context, message, CB_CHANGE_ATTRIBUTE)

    def _simple_change_ack(
        self,
        context: HandlerContext,
        message: SnapMessage,
        callback_id: int,
    ) -> list[SnapMessage]:
        session = _resolve_session(context, message)
        if session is None:
            return []
        acknowledge_number = _ack_for_request(message, session)

        payload = struct.pack('>2L', callback_id, 0)
        return [
            context.reply(
                message,
                type_flags=CHANNEL_ROOM | FLAG_RESPONSE,
                command=commands.CMD_RESULT_WRAPPER,
                payload=payload,
                session_id=session.session_id,
                acknowledge_number=acknowledge_number,
            )
        ]

    def _build_multi_lobby_user_query_payload(self, context: HandlerContext, message: SnapMessage) -> bytes:
        # snapsi keeps this first entry special: lobby id 1 uses the count from lobby 0.
        payload = struct.pack('>L4sL', 1, USER_ATTRIBUTE_TOKEN, context.sessions.count_users_in_lobby(0))

        # Keep snapsi's embedded-entry header word (0x501C), packet numbering, and id range.
        follow_up_size_word = 0x501C
        for lobby_id in range(1, 0x13):
            payload += struct.pack(
                '>HBB3LL4sL',
                follow_up_size_word,
                lobby_id & 0xFF,
                commands.CMD_QUERY_ATTRIBUTE,
                message.session_id,
                0,
                message.sequence_number,
                lobby_id,
                USER_ATTRIBUTE_TOKEN,
                context.sessions.count_users_in_lobby(lobby_id),
            )
        return payload


def _resolve_session(context: HandlerContext, message: SnapMessage) -> Session | None:
    """Find session by id first, then by endpoint."""

    session = context.sessions.get(message.session_id)
    if session is not None:
        return session
    return context.sessions.get_by_endpoint(message.endpoint)


def _prune_stale_rooms_in_lobby(context: HandlerContext, lobby_id: int) -> None:
    """Remove stale room members and empty rooms in one lobby."""

    for room in context.rooms.list_for_lobby(lobby_id):
        _prune_stale_room_members(context, room.room_id)


def _prune_stale_room_members(context: HandlerContext, room_id: int) -> None:
    """Remove room members whose session state no longer points to this room."""

    room = context.rooms.get(room_id)
    if room is None:
        return

    for member_session_id in tuple(room.members):
        member_session = context.sessions.get(member_session_id)
        if member_session is None or member_session.room_id == 0:
            context.rooms.leave(room_id, member_session_id)


def _pack_fixed(value: str, size: int) -> bytes:
    """Pack text into fixed-size null-padded bytes."""

    encoded = value.encode('utf-8', errors='ignore')[:size]
    return struct.pack(f'{size}s', encoded)


def _network_username(value: str) -> str:
    """Match observed username wire format used in snapsi captures."""

    if value.endswith('\n'):
        return value
    return f'{value}\n'


def _network_team(value: str) -> str:
    """Avoid empty team strings in room/user callback payloads."""

    if value:
        return value
    return 'team'


def _build_chat_echo_payload(session: Session, payload: bytes) -> bytes:
    """Mirror chat payload with user identity."""

    username = session.username
    team = 'team'
    message = 'openSNAP message.'

    if len(payload) >= 2:
        user_len = payload[0]
        team_len = payload[1]
        user_start = 2
        team_start = user_start + user_len
        body_start = team_start + team_len
        if body_start <= len(payload):
            username = payload[user_start:team_start].decode('utf-8', errors='ignore')
            team = payload[team_start:body_start].decode('utf-8', errors='ignore')
            message = payload[body_start:].decode('utf-8', errors='ignore') or message

    username_bytes = username.encode('utf-8')
    team_bytes = team.encode('utf-8')
    message_bytes = message.encode('utf-8')
    return struct.pack(
        f'>2B{len(username_bytes)}s{len(team_bytes)}s{len(message_bytes)}s',
        len(username_bytes),
        len(team_bytes),
        username_bytes,
        team_bytes,
        message_bytes,
    )


def _broadcast_lobby_chat(
    context: HandlerContext,
    lobby_id: int,
    payload: bytes,
    exclude_session_id: int | None = None,
) -> list[SnapMessage]:
    """Create one lobby chat callback packet per lobby member."""

    if lobby_id <= 0:
        return []

    messages: list[SnapMessage] = []
    for member in context.sessions.list_lobby_members(lobby_id):
        if exclude_session_id is not None and member.session_id == exclude_session_id:
            continue
        messages.append(
            context.direct(
                endpoint=member.endpoint,
                session_id=member.session_id,
                type_flags=0x1400 | FLAG_RESPONSE,
                command=commands.CMD_SEND,
                payload=payload,
                acknowledge_number=_ack_for_session(member),
            )
        )
    return messages


def _broadcast_room_chat(
    context: HandlerContext,
    room_id: int,
    payload: bytes,
    exclude_session_id: int | None = None,
) -> list[SnapMessage]:
    """Create one room chat callback packet per room member."""

    if room_id <= 0:
        return []

    messages: list[SnapMessage] = []
    for member in context.sessions.list_room_members(room_id):
        if exclude_session_id is not None and member.session_id == exclude_session_id:
            continue
        messages.append(
            context.direct(
                endpoint=member.endpoint,
                session_id=member.session_id,
                type_flags=0x2400 | FLAG_RESPONSE,
                command=commands.CMD_SEND,
                payload=payload,
                acknowledge_number=_ack_for_session(member),
            )
        )
    return messages


def _broadcast_room_game_packet(
    *,
    context: HandlerContext,
    request: SnapMessage,
    room_id: int,
    payload: bytes,
    exclude_session_id: int | None,
    type_flags: int | None = None,
) -> list[SnapMessage]:
    """Relay game packet to other room members."""

    if room_id <= 0:
        return []

    outbound_type_flags = request.type_flags if type_flags is None else type_flags

    messages: list[SnapMessage] = []
    for member in context.sessions.list_room_members(room_id):
        if exclude_session_id is not None and member.session_id == exclude_session_id:
            continue

        messages.append(
            context.direct(
                endpoint=member.endpoint,
                session_id=member.session_id,
                type_flags=outbound_type_flags,
                command=commands.CMD_SEND,
                payload=payload,
                packet_number=request.packet_number,
                acknowledge_number=_ack_for_session(member),
            )
        )
    return messages


def _build_send_target_payload(payload: bytes) -> bytes | None:
    """Build relay payload for send-target callbacks.

    Binary/capture parity: server relays sender payload and only zeroes the
    target-session field at `+0x04` before forwarding to the destination client.
    """

    if len(payload) < 10:
        return None
    return payload[:4] + b'\x00\x00\x00\x00' + payload[8:]


def _should_include_sender_for_room_game_payload(payload: bytes) -> bool:
    """Return True when room-game payloads must be echoed back to sender.

    Binary parity:
    - Start-game trigger uses subcommand 0x8001 and must relay to all members.
    - End-of-game flow uses two CMD_SEND families that are consumed per-slot:
      - 0x0658..0x065f (end marker)
      - 0x1468..0x146f (finish marker)
    """

    if len(payload) < 2:
        return False

    subcommand = get_u16(payload, 0)
    if subcommand == 0x8001:
        return True

    return (0x0658 <= subcommand <= 0x065F) or (0x1468 <= subcommand <= 0x146F)


def _build_room_join_callbacks(
    *,
    context: HandlerContext,
    joining_session: Session,
    recipients: list[Session],
) -> list[SnapMessage]:
    """Notify existing room members that a player joined.

    Keep one callback per recipient and preserve legacy callback channel flags.
    """

    account = context.accounts.get_by_id(joining_session.user_id)
    team = _network_team('' if account is None else account.team)
    payload = struct.pack(
        '>16s2L16s',
        _pack_fixed(_network_username(joining_session.username), 16),
        joining_session.session_id,
        0,
        _pack_fixed(team, 16),
    )

    messages: list[SnapMessage] = []
    for member in recipients:
        if member.session_id == joining_session.session_id:
            continue
        messages.append(
            context.direct(
                endpoint=member.endpoint,
                session_id=member.session_id,
                # Keep callback-channel semantics while making join callbacks
                # reliable so lost host notifications do not stall "Getting
                # information" progression.
                type_flags=CHANNEL_ROOM | FLAG_RESPONSE | FLAG_RELIABLE,
                command=commands.CMD_JOIN,
                payload=payload,
                acknowledge_number=_ack_for_session(member),
            )
        )
    return messages


def _build_room_leave_callbacks(
    *,
    context: HandlerContext,
    leaving_session_id: int,
    recipients: list[Session],
) -> list[SnapMessage]:
    """Notify remaining room members that one peer left the room.

    Use reliable callback-channel packets so end-of-game room teardown does not
    depend on a single lossy datagram.
    """

    payload = struct.pack('>L', leaving_session_id)
    messages: list[SnapMessage] = []
    for member in recipients:
        if member.session_id == leaving_session_id:
            continue
        messages.append(
            context.direct(
                endpoint=member.endpoint,
                session_id=member.session_id,
                type_flags=CHANNEL_ROOM | FLAG_RESPONSE | FLAG_RELIABLE,
                command=commands.CMD_LEAVE,
                payload=payload,
                acknowledge_number=_ack_for_session(member),
            )
        )
    return messages


def _ack_for_session(session: Session) -> int:
    """ACK value for unsolicited packets sent to one client session."""

    if session.last_incoming_sequence < 0:
        return 0
    return session.last_incoming_sequence


def _ack_for_request(request: SnapMessage, session: Session) -> int:
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
