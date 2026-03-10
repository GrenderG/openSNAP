"""Auto Modellista game plugin."""

from dataclasses import dataclass, replace
import logging
import struct

from opensnap.core.context import HandlerContext
from opensnap.core.router import CommandRouter
from opensnap.core.sessions import Session
from opensnap.plugins.base import GamePlugin
from opensnap.protocol import (
    GameTags,
    PostGameReportMask,
    RoomSubcommand,
    RoomGameState,
    commands,
)
from opensnap.protocol.constants import (
    FLAG_CHANNEL_BITS,
    FLAG_ROOM,
    FLAG_MULTI,
    FLAG_RELIABLE,
    FLAG_RESPONSE,
    RELAY_CONTEXT_MASK,
    RESULT_WRAPPER_STATUS_ERROR_DIALOG,
    RESULT_WRAPPER_STATUS_OK,
    TYPE_LOBBY_RELAY_REQUEST,
    TYPE_LOBBY_RELAY,
    TYPE_ROOM_RELAY,
)
from opensnap.protocol.fields import get_c_string, get_u16, get_u32
from opensnap.protocol.models import SnapMessage

_LOGGER = logging.getLogger('opensnap.plugins.automodellista')
# Binary-verified attribute selector used by kkQueryLobbyAttribute/kkQueryGameRoomAttribute:
# SLUS_206.42 cpnGetJoinUserLobby/cpnGetJoinUserRoom load 0x55534552 ("USER").
USER_ATTRIBUTE_TOKEN = b'USER'
PENDING_ROOM_JOIN_RETRY_TICKS = 3
PENDING_ROOM_JOIN_MAX_RETRIES = 3
# Event create-race uses this client-side sentinel when password is disabled.
# Keep room creation semantics as "no password", not literal sentinel text.
ROOM_PASSWORD_EMPTY_SENTINELS = frozenset({'no pw'})


@dataclass(slots=True)
class _PendingRoomJoin:
    """Tracked room join waiting for the guest-side sync to begin."""

    room_id: int
    ticks_until_retry: int = PENDING_ROOM_JOIN_RETRY_TICKS
    retries_remaining: int = PENDING_ROOM_JOIN_MAX_RETRIES


class AutoModellistaPlugin(GamePlugin):
    """Command handlers for Auto Modellista behavior."""

    name = 'automodellista'

    def __init__(self) -> None:
        # Retransmitted reliable create-room packets reuse sequence numbers.
        self._create_room_results: dict[tuple[int, int], int] = {}
        # Exact reliable room-join retries must replay the original wrapper and
        # host callback sequence numbers so the client can recover from a lost
        # first callback without double-inserting the guest.
        self._latest_reliable_join_result: dict[int, tuple[int, int, tuple[SnapMessage, ...]]] = {}
        # Reliable leave retries must replay the same wrapper sequence so the
        # client treats them as duplicates instead of a fresh callback.
        self._latest_reliable_leave_result: dict[tuple[int, int], tuple[int, SnapMessage]] = {}
        self._room_game_states: dict[int, RoomGameState] = {}
        # Track which post-game packet families each room member has reported
        # for the current game. Keys are room_id -> {session_id: bitmask}.
        self._post_game_reports: dict[int, dict[int, PostGameReportMask]] = {}
        # Track guest joins until the joiner confirms room sync (`0x8102` / `0x8008`).
        self._pending_room_joins: dict[int, _PendingRoomJoin] = {}

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
        """Run periodic game-specific recovery tasks."""

        messages: list[SnapMessage] = []
        for joining_session_id in tuple(self._pending_room_joins):
            pending = self._pending_room_joins.get(joining_session_id)
            if pending is None:
                continue

            joining_session = context.sessions.get(joining_session_id)
            if joining_session is None or joining_session.room_id != pending.room_id:
                self._pending_room_joins.pop(joining_session_id, None)
                continue

            recipients = [
                member for member in context.sessions.list_room_members(pending.room_id)
                if member.session_id != joining_session_id
            ]
            if not recipients:
                self._pending_room_joins.pop(joining_session_id, None)
                continue

            if pending.ticks_until_retry > 0:
                pending.ticks_until_retry -= 1
                continue

            messages.extend(
                _build_room_join_callbacks(
                    context=context,
                    joining_session=joining_session,
                    recipients=recipients,
                )
            )
            if pending.retries_remaining <= 1:
                self._pending_room_joins.pop(joining_session_id, None)
            else:
                pending.retries_remaining -= 1
                pending.ticks_until_retry = PENDING_ROOM_JOIN_RETRY_TICKS

        return messages

    def on_session_timeout(self, context: HandlerContext, session: Session) -> list[SnapMessage]:
        """Clean up room state after one client-side transport timeout."""

        self._clear_session_join_retry_state(session.session_id)
        self._latest_reliable_leave_result.pop((session.session_id, FLAG_CHANNEL_BITS), None)
        self._latest_reliable_leave_result.pop((session.session_id, FLAG_ROOM), None)

        room_id = session.room_id
        if room_id <= 0:
            return []

        room = context.rooms.get(room_id)
        if room is None:
            return []

        self._reset_post_game_state(room_id)
        if session.session_id == room.host_session_id:
            self._clear_pending_room_joins_for_room(room_id)
            for member in context.sessions.list_room_members(room_id):
                if member.session_id != session.session_id:
                    context.sessions.set_room(member.session_id, 0)
            for member_session_id in tuple(room.members):
                context.rooms.leave(room_id, member_session_id)
            return []

        recipients = [
            member for member in context.sessions.list_room_members(room_id)
            if member.session_id != session.session_id
        ]
        context.rooms.leave(room_id, session.session_id)
        return _build_room_leave_callbacks(
            context=context,
            leaving_session_id=session.session_id,
            recipients=recipients,
        )

    def _handle_query_lobbies(self, context: HandlerContext, message: SnapMessage) -> list[SnapMessage]:
        entries = []
        for lobby in context.lobbies.list()[: context.config.server.max_lobbies]:
            users_in = context.sessions.count_users_in_lobby(lobby.lobby_id)
            entries.append(struct.pack('>16s3L', _pack_fixed(lobby.name, 16), users_in, 0, lobby.lobby_id))

        payload = struct.pack('>3L', 0, 1, len(entries)) + b''.join(entries)
        return [
            context.reply(
                message,
                type_flags=FLAG_CHANNEL_BITS | FLAG_RESPONSE,
                command=commands.CMD_QUERY_LOBBIES,
                payload=payload,
            )
        ]

    def _handle_query_attribute(self, context: HandlerContext, message: SnapMessage) -> list[SnapMessage]:
        channel_type = message.type_flags & FLAG_CHANNEL_BITS

        if (
            message.embedded_in_multi
            and channel_type == FLAG_CHANNEL_BITS
            and len(message.payload) >= 8
            and message.payload[4:8] == USER_ATTRIBUTE_TOKEN
        ):
            return []

        if (message.type_flags & FLAG_MULTI) and channel_type == FLAG_CHANNEL_BITS:
            # Keep behavior where one multi-packet contains lobby USER counts.
            payload = self._build_multi_lobby_user_query_payload(context, message)
            type_flags = FLAG_CHANNEL_BITS | FLAG_RESPONSE | FLAG_MULTI
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

        channel_id = get_u32(message.payload, 0)
        users_in_channel = 0

        if channel_type == FLAG_ROOM:
            _prune_stale_room_members(context, channel_id)
            room = context.rooms.get(channel_id)
            if room is not None:
                users_in_channel = len(room.members)
        elif _is_allowed_lobby_id(context, channel_id):
            users_in_channel = context.sessions.count_users_in_lobby(channel_id)

        payload = struct.pack('>L4sL', channel_id, USER_ATTRIBUTE_TOKEN, users_in_channel)
        return [
            context.reply(
                message,
                type_flags=channel_type | FLAG_RESPONSE,
                command=commands.CMD_QUERY_ATTRIBUTE,
                payload=payload,
            )
        ]

    def _handle_query_game_rooms(self, context: HandlerContext, message: SnapMessage) -> list[SnapMessage]:
        lobby_id = get_u32(message.payload, 0)
        rooms = []
        if _is_allowed_lobby_id(context, lobby_id):
            _prune_stale_rooms_in_lobby(context, lobby_id)
            rooms = context.rooms.list_for_lobby(lobby_id)
        entries = []
        for room in rooms:
            advertised_max_players = min(room.max_players, context.config.server.max_players_per_room)
            entries.append(
                struct.pack(
                    '>16s5L',
                    _pack_fixed(room.name, 16),
                    len(room.members),
                    0,
                    room.rules,
                    advertised_max_players,
                    room.room_id,
                )
            )

        payload = struct.pack('>3L', 0, 1, len(entries)) + b''.join(entries)
        return [
            context.reply(
                message,
                type_flags=FLAG_CHANNEL_BITS | FLAG_RESPONSE,
                command=commands.CMD_QUERY_GAME_ROOMS,
                payload=payload,
            )
        ]

    def _handle_query_user(self, context: HandlerContext, message: SnapMessage) -> list[SnapMessage]:
        if (message.type_flags & FLAG_CHANNEL_BITS) != FLAG_ROOM:
            _LOGGER.warning(
                'Rejecting query-user command from %s:%d: unsupported channel type=0x%04x.',
                message.endpoint.host,
                message.endpoint.port,
                message.type_flags,
            )
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
                type_flags=FLAG_ROOM | FLAG_RESPONSE,
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
                    type_flags=FLAG_ROOM | FLAG_RESPONSE,
                    command=commands.CMD_RESULT_WRAPPER,
                    payload=struct.pack('>2L', GameTags.START_OK, cached_result),
                    session_id=session.session_id,
                )
            ]

        if not _is_allowed_lobby_id(context, session.lobby_id):
            self._create_room_results[cache_key] = RESULT_WRAPPER_STATUS_ERROR_DIALOG
            return [
                context.reply(
                    message,
                    type_flags=FLAG_ROOM | FLAG_RESPONSE,
                    command=commands.CMD_RESULT_WRAPPER,
                    payload=struct.pack('>2L', GameTags.START_OK, RESULT_WRAPPER_STATUS_ERROR_DIALOG),
                    session_id=session.session_id,
                )
            ]

        _prune_stale_rooms_in_lobby(context, session.lobby_id)
        room_name = get_c_string(message.payload, 0)
        room_password = _normalize_room_password(get_c_string(message.payload, 0x14))
        requested_max_players = max(get_u32(message.payload, 0x10), 1)
        max_players = min(requested_max_players, context.config.server.max_players_per_room)
        rules = get_u32(message.payload, 0x28)

        room_count = len(context.rooms.list_for_lobby(session.lobby_id))
        if room_count >= context.config.server.max_rooms_per_lobby:
            self._create_room_results[cache_key] = RESULT_WRAPPER_STATUS_ERROR_DIALOG
            return [
                context.reply(
                    message,
                    type_flags=FLAG_ROOM | FLAG_RESPONSE,
                    command=commands.CMD_RESULT_WRAPPER,
                    payload=struct.pack('>2L', GameTags.START_OK, RESULT_WRAPPER_STATUS_ERROR_DIALOG),
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
        self._reset_post_game_state(room.room_id)
        self._create_room_results[cache_key] = room.room_id

        payload = struct.pack('>2L', GameTags.START_OK, room.room_id)
        return [
            context.reply(
                message,
                type_flags=FLAG_ROOM | FLAG_RESPONSE,
                command=commands.CMD_RESULT_WRAPPER,
                payload=payload,
                session_id=session.session_id,
            )
        ]

    def _handle_join(self, context: HandlerContext, message: SnapMessage) -> list[SnapMessage]:
        session = _resolve_session(context, message)
        if session is None:
            return []

        if (message.type_flags & FLAG_CHANNEL_BITS) == FLAG_CHANNEL_BITS:
            if session.room_id > 0:
                context.rooms.leave(session.room_id, session.session_id)
                context.sessions.set_room(session.session_id, 0)
            lobby_id = get_u32(message.payload, 0)
            if not _is_allowed_lobby_id(context, lobby_id):
                payload = struct.pack('>2L', GameTags.GAME_START, RESULT_WRAPPER_STATUS_ERROR_DIALOG)
                return [
                    context.reply(
                        message,
                        type_flags=FLAG_CHANNEL_BITS | FLAG_RESPONSE,
                        command=commands.CMD_RESULT_WRAPPER,
                        payload=payload,
                        session_id=session.session_id,
                    )
                ]
            context.sessions.set_lobby(session.session_id, lobby_id)
            context.sessions.set_room(session.session_id, 0)
            payload = struct.pack('>2L', GameTags.GAME_START, RESULT_WRAPPER_STATUS_OK)
            return [
                context.reply(
                    message,
                    type_flags=FLAG_CHANNEL_BITS | FLAG_RESPONSE,
                    command=commands.CMD_RESULT_WRAPPER,
                    payload=payload,
                    session_id=session.session_id,
                )
            ]

        if (message.type_flags & FLAG_CHANNEL_BITS) == FLAG_ROOM:
            room_id = get_u32(message.payload, 0)
            _prune_stale_room_members(context, room_id)
            room = context.rooms.get(room_id)

            # Reliable join retransmits are expected on packet loss.
            # If the same reliable join is retried after the guest is already in
            # the room, replay the original response bundle with the original
            # sequence numbers. That makes the retry safe for the host if the
            # first callback was lost.
            cached_retry = self._cached_reliable_join_result(message, session, room_id)
            if cached_retry is not None:
                return cached_retry

            # Other already-in-room joins stay idempotent and return wrapper-only.
            if room is not None and session.room_id == room_id and session.session_id in room.members:
                payload = struct.pack('>2L', GameTags.GAME_START, RESULT_WRAPPER_STATUS_OK)
                return [
                    context.reply(
                        message,
                        type_flags=FLAG_ROOM | FLAG_RESPONSE,
                        command=commands.CMD_RESULT_WRAPPER,
                        payload=payload,
                        session_id=session.session_id,
                    )
                ]

            existing_members = [
                member for member in context.sessions.list_room_members(room_id)
                if member.session_id != session.session_id
            ]
            success = False
            if room is not None:
                effective_capacity = min(room.max_players, context.config.server.max_players_per_room)
                if session.session_id in room.members or len(room.members) < effective_capacity:
                    success = context.rooms.join(room_id, session.session_id)
            if success:
                context.sessions.set_room(session.session_id, room_id)
                self._reset_post_game_state(room_id)
                callbacks = _build_room_join_callbacks(
                    context=context,
                    joining_session=session,
                    recipients=existing_members,
                )
                if callbacks:
                    self._pending_room_joins[session.session_id] = _PendingRoomJoin(room_id=room_id)
                else:
                    self._pending_room_joins.pop(session.session_id, None)
                payload = struct.pack('>2L', GameTags.GAME_START, RESULT_WRAPPER_STATUS_OK)
            else:
                callbacks = []
                self._clear_session_join_retry_state(session.session_id)
                payload = struct.pack('>2L', GameTags.GAME_START, RESULT_WRAPPER_STATUS_ERROR_DIALOG)

            outbound = [
                context.reply(
                    message,
                    type_flags=FLAG_ROOM | FLAG_RESPONSE,
                    command=commands.CMD_RESULT_WRAPPER,
                    payload=payload,
                    session_id=session.session_id,
                )
            ] + callbacks
            self._remember_reliable_join_result(message, session, room_id, outbound)
            return outbound

        _LOGGER.warning(
            'Rejecting join command from %s:%d: unsupported channel type=0x%04x.',
            message.endpoint.host,
            message.endpoint.port,
            message.type_flags,
        )
        return []

    def _handle_leave(self, context: HandlerContext, message: SnapMessage) -> list[SnapMessage]:
        session = _resolve_session(context, message)
        if session is None:
            return []

        cached_result = self._cached_reliable_leave_result(message, session)
        if cached_result is not None:
            return [cached_result]

        acknowledge_number = _ack_for_request(message, session)

        if (message.type_flags & FLAG_CHANNEL_BITS) == FLAG_CHANNEL_BITS:
            self._clear_session_join_retry_state(session.session_id)
            callbacks: list[SnapMessage] = []
            if session.room_id > 0:
                room_id = session.room_id
                recipients = [
                    member for member in context.sessions.list_room_members(room_id)
                    if member.session_id != session.session_id
                ]
                self._reset_post_game_state(room_id)
                context.rooms.leave(room_id, session.session_id)
                context.sessions.set_room(session.session_id, 0)
                callbacks = _build_room_leave_callbacks(
                    context=context,
                    leaving_session_id=session.session_id,
                    recipients=recipients,
                )
            context.sessions.set_lobby(session.session_id, 0)
            # Even during the post-game return path, a real CMD_LEAVE must keep
            # the leave callback selector. Reusing the join selector here sends
            # the client down the join-room callback path ("Getting information").
            payload = struct.pack('>2L', GameTags.GAME_OVER, RESULT_WRAPPER_STATUS_OK)
            result_wrapper = context.reply(
                message,
                type_flags=FLAG_CHANNEL_BITS | FLAG_RESPONSE,
                command=commands.CMD_RESULT_WRAPPER,
                payload=payload,
                session_id=session.session_id,
                acknowledge_number=acknowledge_number,
            )
            self._remember_reliable_leave_result(message, session, result_wrapper)
            return [result_wrapper] + callbacks

        if (message.type_flags & FLAG_CHANNEL_BITS) == FLAG_ROOM:
            self._clear_session_join_retry_state(session.session_id)
            callbacks: list[SnapMessage] = []
            room_id = session.room_id
            if room_id > 0:
                recipients = [
                    member for member in context.sessions.list_room_members(room_id)
                    if member.session_id != session.session_id
                ]
                self._reset_post_game_state(room_id)
                context.rooms.leave(room_id, session.session_id)
                context.sessions.set_room(session.session_id, 0)
                callbacks = _build_room_leave_callbacks(
                    context=context,
                    leaving_session_id=session.session_id,
                    recipients=recipients,
                )
            # Room leave keeps the leave selector in both manual and post-game
            # flows. The post-game distinction is carried by the earlier room
            # transition packet (`0x8009`), not by changing the 0x28 selector.
            payload = struct.pack('>2L', GameTags.GAME_OVER, RESULT_WRAPPER_STATUS_OK)
            result_wrapper = context.reply(
                message,
                type_flags=FLAG_ROOM | FLAG_RESPONSE,
                command=commands.CMD_RESULT_WRAPPER,
                payload=payload,
                session_id=session.session_id,
                acknowledge_number=acknowledge_number,
            )
            self._remember_reliable_leave_result(message, session, result_wrapper)
            return [result_wrapper] + callbacks

        _LOGGER.warning(
            'Rejecting leave command from %s:%d: unsupported channel type=0x%04x.',
            message.endpoint.host,
            message.endpoint.port,
            message.type_flags,
        )
        return []

    def _cached_reliable_join_result(
        self,
        message: SnapMessage,
        session: Session,
        room_id: int,
    ) -> list[SnapMessage] | None:
        """Replay the original room-join response bundle for an exact retry."""

        if (message.type_flags & FLAG_RELIABLE) == 0:
            return None
        if message.embedded_in_multi and message.sequence_number == 0:
            return None
        if session.room_id != room_id:
            return None

        cached = self._latest_reliable_join_result.get(session.session_id)
        if cached is None:
            return None

        cached_room_id, cached_sequence, cached_messages = cached
        if cached_room_id != room_id or cached_sequence != message.sequence_number:
            return None

        replay: list[SnapMessage] = []
        for cached_message in cached_messages:
            if cached_message.session_id == session.session_id:
                replay.append(
                    replace(
                        cached_message,
                        endpoint=message.endpoint,
                        session_id=session.session_id,
                    )
                )
                continue
            replay.append(replace(cached_message))
        return replay

    def _remember_reliable_join_result(
        self,
        message: SnapMessage,
        session: Session,
        room_id: int,
        outbound: list[SnapMessage],
    ) -> None:
        """Remember one successful reliable room-join response bundle."""

        if (message.type_flags & FLAG_RELIABLE) == 0:
            self._latest_reliable_join_result.pop(session.session_id, None)
            return
        if message.embedded_in_multi and message.sequence_number == 0:
            self._latest_reliable_join_result.pop(session.session_id, None)
            return

        self._latest_reliable_join_result[session.session_id] = (
            room_id,
            message.sequence_number,
            tuple(replace(outbound_message) for outbound_message in outbound),
        )

    def _cached_reliable_leave_result(self, message: SnapMessage, session: Session) -> SnapMessage | None:
        """Replay the original wrapper for an exact reliable leave retry."""

        if (message.type_flags & FLAG_RELIABLE) == 0:
            return None
        if message.embedded_in_multi and message.sequence_number == 0:
            return None

        key = (session.session_id, message.type_flags & FLAG_CHANNEL_BITS)
        cached = self._latest_reliable_leave_result.get(key)
        if cached is None:
            return None

        cached_sequence, cached_result = cached
        if cached_sequence != message.sequence_number:
            return None

        return replace(
            cached_result,
            endpoint=message.endpoint,
            session_id=session.session_id,
        )

    def _remember_reliable_leave_result(
        self,
        message: SnapMessage,
        session: Session,
        result_wrapper: SnapMessage,
    ) -> None:
        """Remember one reliable leave wrapper for exact retry replay."""

        if (message.type_flags & FLAG_RELIABLE) == 0:
            return
        if message.embedded_in_multi and message.sequence_number == 0:
            return

        key = (session.session_id, message.type_flags & FLAG_CHANNEL_BITS)
        self._latest_reliable_leave_result[key] = (message.sequence_number, result_wrapper)

    def _handle_send(self, context: HandlerContext, message: SnapMessage) -> list[SnapMessage]:
        session = _resolve_session(context, message)
        if session is None:
            return []

        # `SLUS_204.98` `kkReceiveExtentCheck` (`0x002ebff0`) and
        # `SLUS_206.42` `kkReceiveExtentCheck` (`0x002f4e9c`) assign transport
        # ACK ownership to the outer reliable packet before they enter
        # `kkRUDPTopMultiMessageHandle`.
        #
        # Beta1 host-exit uses that exact shape: one reliable multi `CMD_SEND`
        # with additional seq=0 embedded `CMD_SEND` relays plus an embedded
        # `CMD_LEAVE`. Sending separate bare ACKs for those embedded seq=0
        # sends makes the client retry the whole bundle and stall in Exit.
        #
        # Keep dispatching the embedded relays, but suppress sender ACKs for
        # those piggybacked seq=0 sends because the outer packet already owns
        # the only transport ACK in the bundle.
        suppress_sender_ack = (
            message.embedded_in_multi
            and message.sequence_number == 0
            and (message.type_flags & FLAG_RELIABLE) != 0
        )

        if message.type_flags & FLAG_MULTI:
            # Observed multi-packet room sends relay the embedded room payload
            # with the same sender/all-members policy as single CMD_SEND.
            channel = message.type_flags & FLAG_CHANNEL_BITS
            if channel == 0:
                channel = FLAG_ROOM
            responses = [context.reply(
                message,
                type_flags=channel | FLAG_RESPONSE,
                command=commands.CMD_ACK,
                session_id=session.session_id,
            )]

            if channel == FLAG_ROOM and len(message.payload) >= 2:
                responses.extend(
                    self._handle_room_game_send(
                        context=context,
                        session=session,
                        request=message,
                        type_flags=(message.type_flags & ~FLAG_MULTI),
                    )
                )

            return responses

        callback_flags = message.type_flags & RELAY_CONTEXT_MASK
        if callback_flags in (TYPE_LOBBY_RELAY, TYPE_LOBBY_RELAY_REQUEST):
            chat_payload = _build_chat_echo_payload(message.payload)
            chats = _broadcast_lobby_chat(
                context,
                session.lobby_id,
                chat_payload,
                exclude_session_id=session.session_id,
            )
            if suppress_sender_ack:
                return chats
            ack_to_sender = context.reply(
                message,
                type_flags=FLAG_CHANNEL_BITS | FLAG_RESPONSE,
                command=commands.CMD_ACK,
                session_id=session.session_id,
            )
            return [ack_to_sender] + chats

        if callback_flags == TYPE_ROOM_RELAY:
            chat_payload = _build_chat_echo_payload(message.payload)
            chats = _broadcast_room_chat(
                context,
                session.room_id,
                chat_payload,
                exclude_session_id=session.session_id,
            )
            if suppress_sender_ack:
                return chats
            ack_to_sender = context.reply(
                message,
                type_flags=FLAG_ROOM | FLAG_RESPONSE,
                command=commands.CMD_ACK,
                session_id=session.session_id,
            )
            return [ack_to_sender] + chats

        if message.type_flags & FLAG_ROOM:
            if len(message.payload) < 2:
                _LOGGER.warning(
                    (
                        'Ignoring short room send payload from %s:%d '
                        '(type=0x%04x len=%d).'
                    ),
                    message.endpoint.host,
                    message.endpoint.port,
                    message.type_flags,
                    len(message.payload),
                )
                if suppress_sender_ack:
                    return []
                return [context.reply(
                    message,
                    type_flags=FLAG_ROOM | FLAG_RESPONSE,
                    command=commands.CMD_ACK,
                    session_id=session.session_id,
                )]

            relays = self._handle_room_game_send(
                context=context,
                session=session,
                request=message,
            )
            if suppress_sender_ack:
                return relays
            ack_to_sender = context.reply(
                message,
                type_flags=FLAG_ROOM | FLAG_RESPONSE,
                command=commands.CMD_ACK,
                session_id=session.session_id,
            )
            return [ack_to_sender] + relays

        _LOGGER.warning(
            'Rejecting send command from %s:%d: unsupported type=0x%04x.',
            message.endpoint.host,
            message.endpoint.port,
            message.type_flags,
        )
        return []

    def _handle_room_game_send(
        self,
        *,
        context: HandlerContext,
        session: Session,
        request: SnapMessage,
        type_flags: int | None = None,
    ) -> list[SnapMessage]:
        """Handle one room game packet using an explicit flow-tag dispatch."""

        if len(request.payload) < 2:
            return []

        subcommand = get_u16(request.payload, 0)
        tag = self.decode_room_game_tag(subcommand)
        include_sender = self.should_echo_room_game_tag(tag)
        exclude_session_id = None if include_sender else session.session_id
        broadcasts = _broadcast_room_game_packet(
            context=context,
            request=request,
            room_id=session.room_id,
            payload=request.payload,
            exclude_session_id=exclude_session_id,
            type_flags=type_flags,
        )
        transitions = self._build_post_game_transition_messages(
            context=context,
            session=session,
            tag=tag,
        )
        return broadcasts + transitions

    def _build_post_game_transition_messages(
        self,
        *,
        context: HandlerContext,
        session: Session,
        tag: GameTags | None,
    ) -> list[SnapMessage]:
        """Emit the binary-verified post-game room transition when all players finish.

        `SLUS_206.42` sends two proven post-game room packet families:
        - `0x0658..0x065f` end markers
        - `0x1468..0x146f` finish/result payloads

        The client sends distinct end-marker and finish/result packet families
        before it enters the room-exit flow. Wait until every room member has
        reported both proven families, then emit room subcommand `0x8009`,
        because `Recv_GamePacket(0x8009)` is the confirmed path into
        `To_RoomExit(2)`.

        `Recv_GamePacket` only decodes the first 16-bit subcommand before taking
        the `0x8009` branch. It does not read any trailing payload bytes in that
        branch, so the exact required wire body is the two-byte `0x8009` value.
        """

        room_id = session.room_id
        if room_id <= 0:
            return []

        if tag is GameTags.GAME_START:
            self._reset_post_game_state(room_id)
            self._room_game_states[room_id] = RoomGameState.SYNC_STARTED
            return []

        room_state = self._room_game_states.get(room_id, RoomGameState.INIT)
        if room_state is RoomGameState.SYNC_STARTED and tag is None:
            self._room_game_states[room_id] = RoomGameState.IN_GAME
            room_state = RoomGameState.IN_GAME

        if room_state is RoomGameState.INIT:
            return []

        if tag is GameTags.GAME_OVER:
            room_state = RoomGameState.GAME_OVER
            self._room_game_states[room_id] = room_state

        if room_state is RoomGameState.RESULT:
            return []

        report_flag = self.post_game_report_mask(tag)
        if report_flag is PostGameReportMask.NONE:
            return []

        _prune_stale_room_members(context, room_id)
        members = context.sessions.list_room_members(room_id)
        if not members:
            self._reset_post_game_state(room_id)
            return []

        active_member_ids = {member.session_id for member in members}
        room_reports = self._post_game_reports.setdefault(room_id, {})
        for member_id in tuple(room_reports):
            if member_id not in active_member_ids:
                room_reports.pop(member_id, None)

        room_reports[session.session_id] = room_reports.get(session.session_id, PostGameReportMask.NONE) | report_flag
        if any(room_reports.get(member_id, PostGameReportMask.NONE) != PostGameReportMask.COMPLETE for member_id in active_member_ids):
            return []

        self._post_game_reports.pop(room_id, None)
        self._room_game_states[room_id] = RoomGameState.RESULT
        transition_payload = struct.pack('>H', RoomSubcommand.RESULT2)
        messages: list[SnapMessage] = []
        for member in members:
            messages.append(
                context.direct(
                    endpoint=member.endpoint,
                    session_id=member.session_id,
                    type_flags=FLAG_ROOM | FLAG_RELIABLE,
                    command=commands.CMD_SEND,
                    payload=transition_payload,
                    acknowledge_number=_ack_for_session(member),
                )
            )
        return messages

    def _reset_post_game_state(self, room_id: int) -> None:
        """Drop tracked post-game state for one room."""

        self._room_game_states.pop(room_id, None)
        self._post_game_reports.pop(room_id, None)

    def _handle_send_target(self, context: HandlerContext, message: SnapMessage) -> list[SnapMessage]:
        session = _resolve_session(context, message)
        if session is None:
            return []

        response = [
            context.reply(
                message,
                type_flags=FLAG_ROOM | FLAG_RESPONSE,
                command=commands.CMD_ACK,
                session_id=session.session_id,
            )
        ]

        if len(message.payload) < 10:
            _LOGGER.warning(
                'Ignoring short send-target payload from %s:%d (len=%d).',
                message.endpoint.host,
                message.endpoint.port,
                len(message.payload),
            )
            return response

        self._clear_pending_room_join_for_send_target(message.payload)

        target_session_id = get_u32(message.payload, 4)
        target = context.sessions.get(target_session_id)
        if target is None:
            _LOGGER.warning(
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
            _LOGGER.warning(
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
                type_flags=FLAG_ROOM | FLAG_RELIABLE,
                command=commands.CMD_SEND_TARGET,
                payload=relay_payload,
                acknowledge_number=_ack_for_session(target),
            )
        )
        return response

    def _handle_change_user_status(self, context: HandlerContext, message: SnapMessage) -> list[SnapMessage]:
        return self._simple_change_ack(context, message, GameTags.RESULT2)

    def _handle_change_user_property(self, context: HandlerContext, message: SnapMessage) -> list[SnapMessage]:
        return self._simple_change_ack(context, message, GameTags.RESULT)

    def _handle_change_attribute(self, context: HandlerContext, message: SnapMessage) -> list[SnapMessage]:
        return self._simple_change_ack(context, message, GameTags.JOIN_OK)

    def _simple_change_ack(
        self,
        context: HandlerContext,
        message: SnapMessage,
        callback_id: GameTags,
    ) -> list[SnapMessage]:
        session = _resolve_session(context, message)
        if session is None:
            return []
        acknowledge_number = _ack_for_request(message, session)

        payload = struct.pack('>2L', callback_id, RESULT_WRAPPER_STATUS_OK)
        return [
            context.reply(
                message,
                type_flags=FLAG_ROOM | FLAG_RESPONSE,
                command=commands.CMD_RESULT_WRAPPER,
                payload=payload,
                session_id=session.session_id,
                acknowledge_number=acknowledge_number,
            )
        ]

    def _clear_pending_room_join_for_send_target(self, payload: bytes) -> None:
        """Clear pending join retries once the joiner-side room sync begins."""

        subcommand = get_u16(payload, 8)
        if subcommand == RoomSubcommand.JOIN_READY:
            self._pending_room_joins.pop(get_u32(payload, 4), None)
            return

        if subcommand == RoomSubcommand.JOIN_GUEST_SYNC and len(payload) >= 14:
            self._pending_room_joins.pop(get_u32(payload, 10), None)

    def _clear_pending_room_joins_for_room(self, room_id: int) -> None:
        """Drop all tracked join retries tied to one room."""

        for joining_session_id, pending in tuple(self._pending_room_joins.items()):
            if pending.room_id == room_id:
                self._clear_session_join_retry_state(joining_session_id)

    def _clear_session_join_retry_state(self, session_id: int) -> None:
        """Drop join-retry transport state for one session."""

        self._pending_room_joins.pop(session_id, None)
        self._latest_reliable_join_result.pop(session_id, None)

    def _build_multi_lobby_user_query_payload(self, context: HandlerContext, message: SnapMessage) -> bytes:
        lobbies = context.lobbies.list()[: context.config.server.max_lobbies]
        if not lobbies:
            return b''

        first_lobby_id = lobbies[0].lobby_id
        payload = struct.pack(
            '>L4sL',
            first_lobby_id,
            USER_ATTRIBUTE_TOKEN,
            context.sessions.count_users_in_lobby(first_lobby_id),
        )

        # Keep the observed embedded-entry header word (0x501C) and mirror the
        # client's burst layout: the outer message carries the first lobby id,
        # then embedded entries continue with the remaining lobbies.
        follow_up_size_word = 0x501C
        for packet_number, lobby in enumerate(lobbies[1:], start=1):
            lobby_id = lobby.lobby_id
            payload += struct.pack(
                '>HBB3LL4sL',
                follow_up_size_word,
                packet_number & 0xFF,
                commands.CMD_QUERY_ATTRIBUTE,
                message.session_id,
                0,
                message.sequence_number,
                lobby_id,
                USER_ATTRIBUTE_TOKEN,
                context.sessions.count_users_in_lobby(lobby_id),
            )
        return payload


def _is_allowed_lobby_id(context: HandlerContext, lobby_id: int) -> bool:
    """Check whether a lobby id is valid under global configured lobby limits."""

    if lobby_id <= 0 or lobby_id > context.config.server.max_lobbies:
        return False
    return context.lobbies.get(lobby_id) is not None


def _resolve_session(context: HandlerContext, message: SnapMessage) -> Session | None:
    """Find session by id first, then by endpoint."""

    session = context.sessions.get(message.session_id)
    if session is not None:
        return session
    session = context.sessions.get_by_endpoint(message.endpoint)
    if session is not None:
        return session
    _LOGGER.warning(
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


def _normalize_room_password(value: str) -> str:
    """Map known client sentinel text to an empty room password."""

    if value.casefold() in ROOM_PASSWORD_EMPTY_SENTINELS:
        return ''
    return value


def _build_chat_echo_payload(payload: bytes) -> bytes:
    """Mirror the original client chat payload unchanged.

    Payload layout:
    - `u8 username_len`
    - `u8 team_len`
    - `username_len` bytes of username
    - `team_len` bytes of team
    - `len(payload) - 2 - username_len - team_len` bytes of message text

    Auto Modellista already sends the full chat record, so the server should
    relay those bytes unchanged instead of rebuilding the structure.
    """

    return payload


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
                type_flags=TYPE_LOBBY_RELAY | FLAG_RESPONSE,
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
                type_flags=TYPE_ROOM_RELAY | FLAG_RESPONSE,
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

def _build_room_join_callbacks(
    *,
    context: HandlerContext,
    joining_session: Session,
    recipients: list[Session],
) -> list[SnapMessage]:
    """Notify existing room members that a player joined."""

    account = context.accounts.get_by_id(joining_session.user_id)
    team = '' if account is None else account.team
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
                # The host does not ACK this callback family, so keep it on
                # the response channel without transport reliability.
                type_flags=FLAG_ROOM | FLAG_RESPONSE,
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
