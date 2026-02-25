"""Auto Modellista game plugin."""

import struct

from opensnap.core.context import HandlerContext
from opensnap.core.router import CommandRouter
from opensnap.core.sessions import Session
from opensnap.protocol import commands
from opensnap.protocol.constants import CHANNEL_LOBBY, CHANNEL_ROOM, FLAG_MULTI, FLAG_RELIABLE, FLAG_RESPONSE
from opensnap.protocol.fields import get_c_string, get_u16, get_u32, get_u8
from opensnap.protocol.models import SnapMessage

MAX_ROOMS_PER_LOBBY = 50


class AutoModellistaPlugin:
    """Command handlers for Auto Modellista behavior."""

    name = 'automodellista'

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
            b'USER',
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
            team = '' if account is None else account.team
            entries.append(
                struct.pack(
                    '>16s2L32s',
                    _pack_fixed(session.username, 16),
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

        room_name = get_c_string(message.payload, 0)
        room_password = get_c_string(message.payload, 0x14)
        max_players = max(get_u32(message.payload, 0x10), 1)
        rules = get_u32(message.payload, 0x28)

        room_count = len(context.rooms.list_for_lobby(session.lobby_id))
        if room_count >= MAX_ROOMS_PER_LOBBY:
            return [
                context.reply(
                    message,
                    type_flags=CHANNEL_ROOM | FLAG_RESPONSE,
                    command=commands.CMD_RESULT_WRAPPER,
                    payload=struct.pack('>2L', 0x04, 1),
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

        payload = struct.pack('>2L', 0x04, room.room_id)
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
            lobby_id = get_u32(message.payload, 0)
            context.sessions.set_lobby(session.session_id, lobby_id)
            context.sessions.set_room(session.session_id, 0)
            payload = struct.pack('>2L', 0x06, 0)
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
            success = context.rooms.join(room_id, session.session_id)
            if success:
                context.sessions.set_room(session.session_id, room_id)
                payload = struct.pack('>2L', 0x06, 0)
            else:
                payload = struct.pack('>2L', 0x06, 1)

            return [
                context.reply(
                    message,
                    type_flags=CHANNEL_ROOM | FLAG_RESPONSE,
                    command=commands.CMD_RESULT_WRAPPER,
                    payload=payload,
                    session_id=session.session_id,
                )
            ]

        return []

    def _handle_leave(self, context: HandlerContext, message: SnapMessage) -> list[SnapMessage]:
        session = _resolve_session(context, message)
        if session is None:
            return []

        if (message.type_flags & 0x3000) == CHANNEL_LOBBY:
            context.sessions.set_lobby(session.session_id, 0)
            payload = struct.pack('>2L', 0x07, 0)
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
            context.rooms.leave(session.room_id, session.session_id)
            context.sessions.set_room(session.session_id, 0)
            payload = struct.pack('>2L', 0x07, 0)
            return [
                context.reply(
                    message,
                    type_flags=CHANNEL_ROOM | FLAG_RESPONSE,
                    command=commands.CMD_RESULT_WRAPPER,
                    payload=payload,
                    session_id=session.session_id,
                )
            ]

        return []

    def _handle_send(self, context: HandlerContext, message: SnapMessage) -> list[SnapMessage]:
        session = _resolve_session(context, message)
        if session is None:
            return []

        if message.type_flags & FLAG_MULTI:
            # Captures use this special case while leaving a room.
            context.rooms.leave(session.room_id, session.session_id)
            context.sessions.set_room(session.session_id, 0)
            payload = struct.pack('>2L', 0x07, 0)
            return [
                context.reply(
                    message,
                    type_flags=CHANNEL_ROOM | FLAG_RESPONSE,
                    command=commands.CMD_RESULT_WRAPPER,
                    payload=payload,
                    session_id=session.session_id,
                )
            ]

        if (message.type_flags & 0x3400) == 0x1400:
            ack_to_sender = context.reply(
                message,
                type_flags=CHANNEL_LOBBY | FLAG_RESPONSE,
                command=commands.CMD_ACK,
                session_id=session.session_id,
            )
            chat_payload = _build_chat_echo_payload(session, message.payload)
            chats = _broadcast_lobby_chat(context, session.lobby_id, chat_payload)
            return [ack_to_sender] + chats

        if (message.type_flags & 0x3400) == 0x2400:
            ack_to_sender = context.reply(
                message,
                type_flags=CHANNEL_ROOM | FLAG_RESPONSE,
                command=commands.CMD_ACK,
                session_id=session.session_id,
            )
            chat_payload = _build_chat_echo_payload(session, message.payload)
            chats = _broadcast_room_chat(context, session.room_id, chat_payload)
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

            subcommand = get_u16(message.payload, 0)
            if subcommand == 0x8006:
                broadcasts = _broadcast_room_game_packet(
                    context=context,
                    request=message,
                    room_id=session.room_id,
                    payload=message.payload,
                    exclude_session_id=session.session_id,
                )
                return [ack_to_sender] + broadcasts

            return [ack_to_sender]

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
            return response

        target_session_id = get_u32(message.payload, 4)
        target = context.sessions.get(target_session_id)
        if target is None:
            return response

        subcommand = get_u16(message.payload, 8)
        relay_payload = _build_send_target_payload(subcommand, message.payload)
        if relay_payload is None:
            return response

        response.append(
            context.direct(
                endpoint=target.endpoint,
                session_id=target.session_id,
                type_flags=CHANNEL_ROOM | FLAG_RELIABLE,
                command=commands.CMD_SEND_TARGET,
                payload=relay_payload,
                acknowledge_number=target.sequence_number,
            )
        )
        return response

    def _handle_change_user_status(self, context: HandlerContext, message: SnapMessage) -> list[SnapMessage]:
        return self._simple_change_ack(context, message, 0x0D)

    def _handle_change_user_property(self, context: HandlerContext, message: SnapMessage) -> list[SnapMessage]:
        return self._simple_change_ack(context, message, 0x0C)

    def _handle_change_attribute(self, context: HandlerContext, message: SnapMessage) -> list[SnapMessage]:
        return self._simple_change_ack(context, message, 0x08)

    def _simple_change_ack(
        self,
        context: HandlerContext,
        message: SnapMessage,
        subcommand: int,
    ) -> list[SnapMessage]:
        session = _resolve_session(context, message)
        if session is None:
            return []

        payload = struct.pack('>2L', subcommand, 0)
        return [
            context.reply(
                message,
                type_flags=CHANNEL_ROOM | FLAG_RESPONSE,
                command=commands.CMD_RESULT_WRAPPER,
                payload=payload,
                session_id=session.session_id,
            )
        ]

    def _build_multi_lobby_user_query_payload(self, context: HandlerContext, message: SnapMessage) -> bytes:
        lobbies = context.lobbies.list()
        if not lobbies:
            return struct.pack('>L4sL', 0, b'USER', 0)

        first_lobby = lobbies[0]
        payload = struct.pack(
            '>L4sL',
            first_lobby.lobby_id,
            b'USER',
            context.sessions.count_users_in_lobby(first_lobby.lobby_id),
        )

        follow_up_size_word = (CHANNEL_LOBBY | FLAG_RESPONSE) | 0x001C
        for index, lobby in enumerate(lobbies[1:], start=1):
            # Keep format where additional USER entries are packed inline.
            payload += struct.pack(
                '>HBB3LL4sL',
                follow_up_size_word,
                index & 0xFF,
                commands.CMD_QUERY_ATTRIBUTE,
                message.session_id,
                0,
                message.sequence_number,
                lobby.lobby_id,
                b'USER',
                context.sessions.count_users_in_lobby(lobby.lobby_id),
            )
        return payload


def _resolve_session(context: HandlerContext, message: SnapMessage) -> Session | None:
    """Find session by id first, then by endpoint."""

    session = context.sessions.get(message.session_id)
    if session is not None:
        return session
    return context.sessions.get_by_endpoint(message.endpoint)


def _pack_fixed(value: str, size: int) -> bytes:
    """Pack text into fixed-size null-padded bytes."""

    encoded = value.encode('utf-8', errors='ignore')[:size]
    return struct.pack(f'{size}s', encoded)


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


def _broadcast_lobby_chat(context: HandlerContext, lobby_id: int, payload: bytes) -> list[SnapMessage]:
    """Create one lobby chat callback packet per lobby member."""

    if lobby_id <= 0:
        return []

    messages: list[SnapMessage] = []
    for member in context.sessions.list_lobby_members(lobby_id):
        messages.append(
            context.direct(
                endpoint=member.endpoint,
                session_id=member.session_id,
                type_flags=0x1400 | FLAG_RESPONSE,
                command=commands.CMD_SEND,
                payload=payload,
                acknowledge_number=member.sequence_number,
            )
        )
    return messages


def _broadcast_room_chat(context: HandlerContext, room_id: int, payload: bytes) -> list[SnapMessage]:
    """Create one room chat callback packet per room member."""

    if room_id <= 0:
        return []

    messages: list[SnapMessage] = []
    for member in context.sessions.list_room_members(room_id):
        messages.append(
            context.direct(
                endpoint=member.endpoint,
                session_id=member.session_id,
                type_flags=0x2400 | FLAG_RESPONSE,
                command=commands.CMD_SEND,
                payload=payload,
                acknowledge_number=member.sequence_number,
            )
        )
    return messages


def _broadcast_room_game_packet(
    *,
    context: HandlerContext,
    request: SnapMessage,
    room_id: int,
    payload: bytes,
    exclude_session_id: int,
) -> list[SnapMessage]:
    """Relay game packet to other room members."""

    if room_id <= 0:
        return []

    messages: list[SnapMessage] = []
    for member in context.sessions.list_room_members(room_id):
        if member.session_id == exclude_session_id:
            continue

        messages.append(
            context.direct(
                endpoint=member.endpoint,
                session_id=member.session_id,
                type_flags=request.type_flags,
                command=commands.CMD_SEND,
                payload=payload,
                packet_number=request.packet_number,
                acknowledge_number=member.sequence_number,
            )
        )
    return messages


def _build_send_target_payload(subcommand: int, payload: bytes) -> bytes | None:
    """Build relay payload for known send-target subcommands."""

    if subcommand == 0x8005 and len(payload) >= 14:
        # Traces use 0x8005 for player-id sync messages.
        player_id = get_u32(payload, 10)
        return struct.pack('>2LHL', 1, 0, 0x8005, player_id)

    if subcommand == 0x8102 and len(payload) >= 15:
        # Traces use 0x8102 for target state updates.
        user_value = get_u32(payload, 10)
        user_flag = get_u8(payload, 14)
        return struct.pack('>2LHLB', 1, 0, 0x8102, user_value, user_flag)

    if subcommand == 0x8008 and len(payload) >= 12:
        # Traces use 0x8008 for small 3-byte target payloads.
        value_1 = get_u8(payload, 9)
        value_2 = get_u8(payload, 10)
        value_3 = get_u8(payload, 11)
        return struct.pack('>2LH3B', 1, 0, 0x8008, value_1, value_2, value_3)

    return None
