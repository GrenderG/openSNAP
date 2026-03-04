"""Auto Modellista game plugin."""

from dataclasses import dataclass
from enum import IntEnum, IntFlag, auto
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
# SLUS_206.42 cpnGetJoinUserLobby/cpnGetJoinUserRoom load 0x55534552 ("USER").
USER_ATTRIBUTE_TOKEN = b'USER'
class GameTags(IntEnum):
    """Complete logical room-game tag set used by the plugin state machine.

    These values are a normalized internal vocabulary. The PS2 client still
    sends raw room subcommands such as `0x8001`, `0x0658..0x065f`,
    `0x1468..0x146f`, and `0x8009`; those are decoded into this enum after
    parsing so the code can keep a complete staged tag model without confusing
    it with the raw SNAP wire values.

    The same numeric values are also used by `CMD_RESULT_WRAPPER` (`0x28`)
    selector word0. `kkDispatchingOperation` (`0x002ee030..0x002ee31c`)
    byte-swaps the first two payload words, then dispatches by selector:
    - `0x06` -> join callbacks (slots 33/34: lobby / game room)
    - `0x07` -> leave callbacks (slots 35/36: lobby / game room)
    """

    SYNC = 0x00
    SYS = 0x01
    SYS2 = 0x02
    SYS_OK = 0x03
    START_OK = 0x04
    READY = 0x05
    GAME_START = 0x06
    GAME_OVER = 0x07
    JOIN_OK = 0x08
    JOIN_NG = 0x09
    PAUSE = 0x0A
    WAIT_OVER = 0x0B
    RESULT = 0x0C
    RESULT2 = 0x0D
    OWNER = 0x0E
    ECHO = 0x0F
    RESET = 0x10
    TIME_OUT = 0x11

class RoomGameState(IntEnum):
    """Tracked server-side room flow phases used by the plugin."""

    INIT = auto()
    SYNC_STARTED = auto()
    IN_GAME = auto()
    GAME_OVER = auto()
    RESULT = auto()


class PostGameReportMask(IntFlag):
    """Tracked post-game report families for one room member."""

    NONE = 0
    GAME_OVER = 0x01
    RESULT = 0x02
    COMPLETE = GAME_OVER | RESULT


ROOM_SUBCOMMAND_GAME_START = 0x8001
ROOM_SUBCOMMAND_RESULT2 = 0x8009
ROOM_SUBCOMMAND_GAME_OVER_MIN = 0x0658
ROOM_SUBCOMMAND_GAME_OVER_MAX = 0x065F
ROOM_SUBCOMMAND_RESULT_MIN = 0x1468
ROOM_SUBCOMMAND_RESULT_MAX = 0x146F
ROOM_SUBCOMMAND_JOIN_HOST_SYNC = 0x8005
ROOM_SUBCOMMAND_JOIN_GUEST_SYNC = 0x8102
ROOM_SUBCOMMAND_JOIN_READY = 0x8008
PENDING_ROOM_JOIN_RETRY_TICKS = 3
PENDING_ROOM_JOIN_MAX_RETRIES = 3


@dataclass(slots=True)
class _PendingRoomJoin:
    """Tracked room join waiting for the host-side sync to begin."""

    room_id: int
    ticks_until_retry: int = PENDING_ROOM_JOIN_RETRY_TICKS
    retries_remaining: int = PENDING_ROOM_JOIN_MAX_RETRIES


class AutoModellistaPlugin:
    """Command handlers for Auto Modellista behavior."""

    name = 'automodellista'

    def __init__(self) -> None:
        # Retransmitted reliable create-room packets reuse sequence numbers.
        self._create_room_results: dict[tuple[int, int], int] = {}
        self._room_game_states: dict[int, RoomGameState] = {}
        # Track which post-game packet families each room member has reported
        # for the current game. Keys are room_id -> {session_id: bitmask}.
        self._post_game_reports: dict[int, dict[int, PostGameReportMask]] = {}
        # Track guest joins until the room sync begins (`0x8005` / `0x8102`).
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
                    payload=struct.pack('>2L', GameTags.START_OK, cached_result),
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
                    payload=struct.pack('>2L', GameTags.START_OK, 1),
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
            payload = struct.pack('>2L', GameTags.GAME_START, 0)
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
            # If the client is already in this room, keep the join idempotent and
            # let the periodic callback retry path recover any missed host callback.
            if room is not None and session.room_id == room_id and session.session_id in room.members:
                payload = struct.pack('>2L', GameTags.GAME_START, 0)
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
                payload = struct.pack('>2L', GameTags.GAME_START, 0)
            else:
                callbacks = []
                self._pending_room_joins.pop(session.session_id, None)
                payload = struct.pack('>2L', GameTags.GAME_START, 1)

            return [
                context.reply(
                    message,
                    type_flags=CHANNEL_ROOM | FLAG_RESPONSE,
                    command=commands.CMD_RESULT_WRAPPER,
                    payload=payload,
                    session_id=session.session_id,
                )
            ] + callbacks

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
        acknowledge_number = _ack_for_request(message, session)

        if (message.type_flags & 0x3000) == CHANNEL_LOBBY:
            self._pending_room_joins.pop(session.session_id, None)
            callbacks: list[SnapMessage] = []
            exit_completions: list[SnapMessage] = []
            if session.room_id > 0:
                room_id = session.room_id
                recipients = [
                    member for member in context.sessions.list_room_members(room_id)
                    if member.session_id != session.session_id
                ]
                was_post_game_transition = (
                    self._room_game_states.get(room_id) is RoomGameState.RESULT
                )
                self._reset_post_game_state(room_id)
                context.rooms.leave(room_id, session.session_id)
                context.sessions.set_room(session.session_id, 0)
                callbacks = _build_room_leave_callbacks(
                    context=context,
                    leaving_session_id=session.session_id,
                    recipients=recipients,
                )
                if was_post_game_transition and recipients:
                    exit_completions = _build_post_game_leave_completion_messages(
                        context=context,
                        recipients=recipients,
                    )
            context.sessions.set_lobby(session.session_id, 0)
            # Even during the post-game return path, a real CMD_LEAVE must keep
            # the leave callback selector. Reusing the join selector here sends
            # the client down the join-room callback path ("Getting information").
            payload = struct.pack('>2L', GameTags.GAME_OVER, 0)
            return [
                context.reply(
                    message,
                    type_flags=CHANNEL_LOBBY | FLAG_RESPONSE,
                    command=commands.CMD_RESULT_WRAPPER,
                    payload=payload,
                    session_id=session.session_id,
                    acknowledge_number=acknowledge_number,
                )
            ] + callbacks + exit_completions

        if (message.type_flags & 0x3000) == CHANNEL_ROOM:
            self._pending_room_joins.pop(session.session_id, None)
            callbacks: list[SnapMessage] = []
            exit_completions: list[SnapMessage] = []
            room_id = session.room_id
            if room_id > 0:
                recipients = [
                    member for member in context.sessions.list_room_members(room_id)
                    if member.session_id != session.session_id
                ]
                was_post_game_transition = (
                    self._room_game_states.get(room_id) is RoomGameState.RESULT
                )
                self._reset_post_game_state(room_id)
                context.rooms.leave(room_id, session.session_id)
                context.sessions.set_room(session.session_id, 0)
                callbacks = _build_room_leave_callbacks(
                    context=context,
                    leaving_session_id=session.session_id,
                    recipients=recipients,
                )
                if was_post_game_transition and recipients:
                    exit_completions = _build_post_game_leave_completion_messages(
                        context=context,
                        recipients=recipients,
                    )
            # Room leave keeps the leave selector in both manual and post-game
            # flows. The post-game distinction is carried by the earlier room
            # transition packet (`0x8009`), not by changing the 0x28 selector.
            payload = struct.pack('>2L', GameTags.GAME_OVER, 0)
            return [
                context.reply(
                    message,
                    type_flags=CHANNEL_ROOM | FLAG_RESPONSE,
                    command=commands.CMD_RESULT_WRAPPER,
                    payload=payload,
                    session_id=session.session_id,
                    acknowledge_number=acknowledge_number,
                )
            ] + callbacks + exit_completions

        _LOGGER.warning(
            'Rejecting leave command from %s:%d: unsupported channel type=0x%04x.',
            message.endpoint.host,
            message.endpoint.port,
            message.type_flags,
        )
        return []

    def _handle_send(self, context: HandlerContext, message: SnapMessage) -> list[SnapMessage]:
        session = _resolve_session(context, message)
        if session is None:
            return []

        if message.type_flags & FLAG_MULTI:
            # Observed multi-packet room sends relay the embedded room payload
            # with the same sender/all-members policy as single CMD_SEND.
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
                responses.extend(
                    self._handle_room_game_send(
                        context=context,
                        session=session,
                        request=message,
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
            chat_payload = _build_chat_echo_payload(message.payload)
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
            chat_payload = _build_chat_echo_payload(message.payload)
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
                return [ack_to_sender]

            return [ack_to_sender] + self._handle_room_game_send(
                context=context,
                session=session,
                request=message,
            )

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
        tag = _decode_game_tag(subcommand)
        include_sender = _should_echo_game_tag(tag)
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

        report_flag = _post_game_report_mask(tag)
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
        transition_payload = struct.pack('>H', ROOM_SUBCOMMAND_RESULT2)
        messages: list[SnapMessage] = []
        for member in members:
            messages.append(
                context.direct(
                    endpoint=member.endpoint,
                    session_id=member.session_id,
                    type_flags=CHANNEL_ROOM | FLAG_RELIABLE,
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
                type_flags=CHANNEL_ROOM | FLAG_RESPONSE,
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
                type_flags=CHANNEL_ROOM | FLAG_RELIABLE,
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

    def _clear_pending_room_join_for_send_target(self, payload: bytes) -> None:
        """Clear pending join retries once the room sync handshake starts."""

        subcommand = get_u16(payload, 8)
        if subcommand in {ROOM_SUBCOMMAND_JOIN_HOST_SYNC, ROOM_SUBCOMMAND_JOIN_READY}:
            self._pending_room_joins.pop(get_u32(payload, 4), None)
            return

        if subcommand == ROOM_SUBCOMMAND_JOIN_GUEST_SYNC and len(payload) >= 14:
            self._pending_room_joins.pop(get_u32(payload, 10), None)

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


def _decode_game_tag(subcommand: int) -> GameTags | None:
    """Map a proven room subcommand into one internal game tag."""

    if subcommand == ROOM_SUBCOMMAND_GAME_START:
        return GameTags.GAME_START
    if ROOM_SUBCOMMAND_GAME_OVER_MIN <= subcommand <= ROOM_SUBCOMMAND_GAME_OVER_MAX:
        return GameTags.GAME_OVER
    if ROOM_SUBCOMMAND_RESULT_MIN <= subcommand <= ROOM_SUBCOMMAND_RESULT_MAX:
        return GameTags.RESULT
    if subcommand == ROOM_SUBCOMMAND_RESULT2:
        return GameTags.RESULT2
    return None


def _post_game_report_mask(tag: GameTags | None) -> PostGameReportMask:
    """Return the tracked post-game report mask for one internal game tag."""

    if tag is GameTags.GAME_OVER:
        return PostGameReportMask.GAME_OVER
    if tag is GameTags.RESULT:
        return PostGameReportMask.RESULT
    return PostGameReportMask.NONE


def _should_echo_game_tag(tag: GameTags | None) -> bool:
    """Return whether one internal game tag should be echoed to sender."""

    return tag is GameTags.GAME_START


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
                type_flags=CHANNEL_ROOM | FLAG_RESPONSE,
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
                type_flags=CHANNEL_ROOM | FLAG_RESPONSE,
                command=commands.CMD_LEAVE,
                payload=payload,
                acknowledge_number=_ack_for_session(member),
            )
        )
    return messages


def _build_post_game_leave_completion_messages(
    *,
    context: HandlerContext,
    recipients: list[Session],
) -> list[SnapMessage]:
    """Send the reliable post-game room completion signal to remaining members.

    `Recv_GamePacket(0x8003)` clears `menu[20360]`. In the current post-game
    traces, keeping only the non-reliable peer `CMD_LEAVE` callback leaves the
    remaining player in the longer timeout path. The leave result wrapper must
    stay on selector `0x07`; `0x06` is the join family and re-enters join-room
    logic. The state-aware post-game bridge is this separate reliable `0x8003`
    packet to whichever room members remain after the first post-game leave.
    """

    payload = struct.pack('>H', 0x8003)
    messages: list[SnapMessage] = []
    for member in recipients:
        messages.append(
            context.direct(
                endpoint=member.endpoint,
                session_id=member.session_id,
                type_flags=CHANNEL_ROOM | FLAG_RELIABLE,
                command=commands.CMD_SEND,
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
