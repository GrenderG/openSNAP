"""Resident Evil Outbreak File#1 game plugin.

Rules below are taken from `SLUS_207.65` (NTSC-U v1 and v2): the lobby overlay
`netwk.bin` (`BIN/3.DAT`, load address `0x00570000`, identical in both builds
for everything cited here) and the main ELF (v2 addresses; v1 is `-0x910`).
The SN@P SDK is the Monster Hunter build (Outbreak = MH NA `- 0x21fc0`).

Scenario overlay: `game.bin` replaces `netwk.bin` at `0x00570000` for a
scenario, but the lobby callbacks (slots 2..13, registered at `0x00583ca0`)
are never cleared, and the SDK receive thread (`0x001cb9a0`) dispatches from
login to logout. A lobby callback reaching a player in a scenario jumps into
`game.bin`. Every client sends status `1` right before its scenario
(`0x0058b4a8`) and status `0xf7e00001` once it has registered its lobby
callbacks again (`0x005842f4`, `0x005843e0`, `0x005849b8`), so lobby callbacks
only go to players outside that window. Scenario traffic is `CMD_SEND` room
broadcasts (`kkSendGamePacket` `0x001e5008`, flags `0x8000` or 0 from
`0x001cbc14`/`0x001cbcb4`).
"""

from collections.abc import Callable
from dataclasses import dataclass
import logging
import struct

from opensnap.core.context import HandlerContext
from opensnap.core.game import handlers as game_handlers
from opensnap.core.rooms import GameRoom
from opensnap.core.router import CommandRouter
from opensnap.core.sessions import Session
from opensnap.plugins.base import GamePlugin, SnapTitle
from opensnap.plugins.common import (
    SEARCH_AT_LEAST,
    SEARCH_AT_MOST,
    SEARCH_EQUAL,
    SEARCH_NAME,
    SEARCH_OID,
    USER_ATTRIBUTE_TOKEN,
    ReliableReplyCache,
    ack_for_request,
    ack_for_session,
    build_room_leave_callbacks,
    build_send_target_payload,
    pack_fixed,
    parse_attribute_search,
    resolve_session,
    search_text,
)
from opensnap.plugins.outbreak.directory import Area, Directory, read_directory
from opensnap.protocol import commands
from opensnap.protocol.constants import (
    FLAG_CHANNEL_BITS,
    FLAG_MULTI,
    FLAG_RELAY,
    FLAG_RELIABLE,
    FLAG_RESPONSE,
    FLAG_ROOM,
    FOOTER_MARKER,
    RELAY_CONTEXT_MASK,
    TYPE_LOBBY_RELAY,
    TYPE_LOBBY_RELAY_REQUEST,
    TYPE_ROOM_RELAY,
)
from opensnap.protocol.fields import get_c_string, get_u32
from opensnap.protocol.models import SnapMessage

_LOGGER = logging.getLogger('opensnap.plugins.outbreak')

# `kkLoginClient` call `0x00584940`: title code `0xCAE0`, 240-byte user
# property (also sent by `kkLoginToKICS` from payload `+0x128`).
TITLE_CODE = 0xCAE0
PROFILE_SIZE = 240
KICS_PROFILE_OFFSET = 0x128

# `kkChangeUserStatus` values.
STATUS_IN_SCENARIO = 1
STATUS_IN_LOBBY = 0xF7E00001

# Rooms. The waiting room keeps four members (roster table `0x00588120`, no
# bound check) and the room list at most 30 rows. `STAT` carries the whole
# rules word; `0x40000000` marks a started or disbanded room, which the list
# skips (`0x00599f20`) and the Friend status shows as in game (`0x0059a4cc`).
MAX_ROOM_PLAYERS = 4
MAX_LISTED_ROOMS = 30
ROOM_CLOSED_FLAG = 0x40000000
STAT_ATTRIBUTE = b'STAT'
MAXI_ATTRIBUTE = b'MAXI'

# Failed results (`CMD_RESULT_ERROR`, `selector, code`). The room join result
# (`0x00587a10`) shows "Incorrect password" for code 15 and a generic refusal
# for any other code.
ERROR_WRONG_PASSWORD = 15
ERROR_REFUSED = 1
RESULT_OK = 0

# The lobby sends no keepalive; scenario clients send a game packet at least
# every second (`0x001c7568`). Five minutes covers the results screen and the
# APP upload before the player is back in the lobby.
SCENARIO_IDLE_LIMIT_SECONDS = 5 * 60
# `kkSearchUsers` request: name16 padded with `-` (`0x0059a6e8`).
SEARCH_NAME_PADDING = '-'
# Search result word `+16` is only tested for zero ("not online", `0x0058ad74`).
SEARCH_ONLINE = 1


@dataclass(slots=True)
class _Player:
    """Outbreak runtime data for one logged-in session."""

    session_id: int
    profile: bytes = bytes(PROFILE_SIZE)
    # Between status `1` and status `0xf7e00001` (see the module docstring).
    in_scenario: bool = False


class OutbreakPlugin(GamePlugin):
    """Command handlers for the Outbreak lobby, rooms and scenarios."""

    name = 'outbreak'
    snap_titles = (SnapTitle(title_code=TITLE_CODE, footer_marker=FOOTER_MARKER, name='Resident Evil Outbreak NA'),)
    companion_app = 'capcom'

    def __init__(self) -> None:
        self._players: dict[int, _Player] = {}
        self._directory: Directory | None = None
        self._reliable_replies = ReliableReplyCache()

    @property
    def directory(self) -> Directory:
        assert self._directory is not None, 'register_handlers() builds the directory'
        return self._directory

    def register_handlers(self, router: CommandRouter, context: HandlerContext) -> None:
        self._directory = read_directory()
        router.register(commands.CMD_LOGIN_TO_KICS, self._tracking_rooms(self._handle_login_to_kics))
        router.register(commands.CMD_LOGOUT_CLIENT, self._tracking_rooms(self._handle_logout))
        router.register(commands.CMD_QUERY_AREA, self._handle_query_area)
        router.register(commands.CMD_QUERY_ATTRIBUTE, self._handle_query_attribute)
        router.register(commands.CMD_QUERY_GAME_ROOMS, self._handle_query_game_rooms)
        router.register(commands.CMD_QUERY_USER, self._handle_query_user)
        router.register(commands.CMD_SEARCH_USERS_BY_NAME, self._handle_search_users)
        router.register(commands.CMD_SEARCH_ROOMS, self._handle_search_rooms)
        router.register(commands.CMD_CREATE_GAME_ROOM, self._tracking_rooms(self._handle_create_game_room))
        router.register(commands.CMD_JOIN, self._tracking_rooms(self._handle_join))
        router.register(commands.CMD_LEAVE, self._tracking_rooms(self._handle_leave))
        router.register(commands.CMD_CHANGE_ATTRIBUTE, self._tracking_rooms(self._handle_change_attribute))
        router.register(commands.CMD_CHANGE_USER_PROPERTY, self._handle_change_user_property)
        router.register(commands.CMD_CHANGE_USER_STATUS, self._handle_change_user_status)
        router.register(commands.CMD_SEND, self._handle_send)
        router.register(commands.CMD_SEND_TARGET, self._handle_send_target)
        router.register(commands.CMD_SEND_ECHO, game_handlers.handle_send_echo)

    def on_session_timeout(self, context: HandlerContext, session: Session) -> list[SnapMessage]:
        """Take a timed-out player out of its room; only lobby peers are told."""

        before = self._listed_rooms(context)
        messages = self._leave_room(context, session)
        self._forget_player(session.session_id)
        return messages + self._room_list_updates(context, before)

    def session_idle_limit(self, context: HandlerContext, session: Session) -> float | None:
        player = self._players.get(session.session_id)
        if player is not None and player.in_scenario:
            return SCENARIO_IDLE_LIMIT_SECONDS
        return None

    # Login.

    def _handle_login_to_kics(self, context: HandlerContext, message: SnapMessage) -> list[SnapMessage]:
        handoff = context.handoffs.get(message.session_id) or context.handoffs.get_by_endpoint(message.endpoint)
        previous = context.sessions.get(message.session_id) or context.sessions.get_by_endpoint(message.endpoint)
        if previous is not None and handoff is not None and previous.session_id == handoff.session_id:
            # The shared handler silently clears room state on a repeated login.
            self._leave_room(context, previous, notify=False)

        responses = game_handlers.handle_login_to_kics(context, message)
        if not responses:
            return responses
        session_id = responses[0].session_id
        profile = message.payload[KICS_PROFILE_OFFSET:KICS_PROFILE_OFFSET + PROFILE_SIZE]
        self._players[session_id] = _Player(
            session_id=session_id,
            profile=profile if len(profile) == PROFILE_SIZE else bytes(PROFILE_SIZE),
        )
        return responses

    def _handle_logout(self, context: HandlerContext, message: SnapMessage) -> list[SnapMessage]:
        session = resolve_session(context, message, _LOGGER)
        if session is None:
            return []
        messages = self._leave_room(context, session)
        context.sessions.set_lobby(session.session_id, 0)
        self._forget_player(session.session_id)
        return messages

    # Areas.

    def _handle_query_area(self, context: HandlerContext, message: SnapMessage) -> list[SnapMessage]:
        """List Areas by `NAME` (equal or range) or `OID`.

        Rows (`0x00588750`, `0x005889f0`): name16, users, capacity, capacity,
        0, Area id. The client also asks each Area's `MAXI` and stores it at
        `+24` (`0x00586b38`), so the capacity fills both words.
        """

        limit, conditions = parse_attribute_search(message.payload)
        areas: tuple[Area, ...] = ()
        low = high = None
        for name, operator, value in conditions:
            if name == SEARCH_NAME and operator == SEARCH_EQUAL:
                area = self.directory.named(search_text(value))
                areas = () if area is None else (area,)
            elif name == SEARCH_OID and operator == SEARCH_EQUAL:
                area = self.directory.area(int.from_bytes(value, 'big'))
                areas = () if area is None else (area,)
            elif name == SEARCH_NAME and operator == SEARCH_AT_LEAST:
                low = search_text(value)
            elif name == SEARCH_NAME and operator == SEARCH_AT_MOST:
                high = search_text(value)
        if low is not None and high is not None:
            areas = self.directory.in_name_range(low, high)

        capacity = self.directory.capacity
        rows = [
            struct.pack(
                '>16s5L', pack_fixed(area.name, 16), self._area_population(context, area.area_id),
                capacity, capacity, 0, area.area_id,
            )
            for area in areas[:limit]
        ]
        return [
            context.reply(
                message,
                type_flags=FLAG_CHANNEL_BITS | FLAG_RESPONSE,
                command=commands.CMD_QUERY_AREA,
                payload=struct.pack('>3L', limit, 1, len(rows)) + b''.join(rows),
            )
        ]

    def _handle_query_attribute(self, context: HandlerContext, message: SnapMessage) -> list[SnapMessage]:
        """Answer `USER` (members) or `MAXI` (capacity) of an Area or a room.

        Reply `id, name, value`; the client checks the name it asked for
        (room `0x00587490`, Area `0x00586ac8`).
        """

        channel_type = message.type_flags & FLAG_CHANNEL_BITS
        channel_id = get_u32(message.payload, 0)
        attribute = message.payload[4:8]
        if channel_type == FLAG_ROOM:
            room = context.rooms.get(channel_id)
            users = 0 if room is None else len(room.members)
            capacity = 0 if room is None else room.max_players
        else:
            users = self._area_population(context, channel_id)
            capacity = self.directory.capacity if self.directory.area(channel_id) is not None else 0
        value = capacity if attribute == MAXI_ATTRIBUTE else users if attribute == USER_ATTRIBUTE_TOKEN else 0
        return [
            context.reply(
                message,
                type_flags=channel_type | FLAG_RESPONSE,
                command=commands.CMD_QUERY_ATTRIBUTE,
                payload=struct.pack('>L4sL', channel_id, attribute, value),
            )
        ]

    # Rooms.

    def _handle_query_game_rooms(self, context: HandlerContext, message: SnapMessage) -> list[SnapMessage]:
        rows = [_room_row(room) for room in self._open_rooms(context, get_u32(message.payload, 0))]
        return [
            context.reply(
                message,
                type_flags=FLAG_CHANNEL_BITS | FLAG_RESPONSE,
                command=commands.CMD_QUERY_GAME_ROOMS,
                payload=struct.pack('>3L', 0, 1, len(rows)) + b''.join(rows),
            )
        ]

    def _handle_query_user(self, context: HandlerContext, message: SnapMessage) -> list[SnapMessage]:
        """Return a room's roster: `name16, sid, length, profile` per member (`0x00588120`)."""

        channel_type = message.type_flags & FLAG_CHANNEL_BITS
        requested_id = get_u32(message.payload, 0)
        room = context.rooms.get(requested_id) if channel_type == FLAG_ROOM else None
        records = []
        for member_id in [] if room is None else sorted(room.members)[:MAX_ROOM_PLAYERS]:
            member = context.sessions.get(member_id)
            if member is not None:
                records.append(self._member_record(member))
        return [
            context.reply(
                message,
                type_flags=channel_type | FLAG_RESPONSE,
                command=commands.CMD_QUERY_USER,
                payload=struct.pack('>3L', requested_id, len(records), len(records)) + b''.join(records),
            )
        ]

    def _handle_create_game_room(self, context: HandlerContext, message: SnapMessage) -> list[SnapMessage]:
        """Create a room in the player's Area.

        Request (44 bytes): name16, `+16` max players, `+20` password16,
        `+36` 1, `+40` rules. The result carries the room id (`selector, id`).
        """

        session = resolve_session(context, message, _LOGGER)
        if session is None:
            return []
        replay = self._reliable_replies.replay(message, session)
        if replay is not None:
            return replay

        area = self.directory.area(session.lobby_id)
        if area is None or len(context.rooms.list_for_lobby(area.area_id)) >= context.config.server.max_rooms_per_lobby:
            outbound = [self._error(context, message, session, ERROR_REFUSED, FLAG_ROOM)]
        else:
            departures = self._leave_room(context, session)
            room = context.rooms.create_room(
                name=get_c_string(message.payload, 0),
                password=get_c_string(message.payload, 0x14),
                rules=get_u32(message.payload, 0x28),
                max_players=min(
                    max(get_u32(message.payload, 0x10), 1),
                    MAX_ROOM_PLAYERS,
                    context.config.server.max_players_per_room,
                ),
                lobby_id=area.area_id,
                host_session_id=session.session_id,
            )
            context.sessions.set_room(session.session_id, room.room_id)
            outbound = departures + [self._result(context, message, session, room.room_id, FLAG_ROOM)]
        self._reliable_replies.remember(message, session, outbound)
        return outbound

    def _handle_join(self, context: HandlerContext, message: SnapMessage) -> list[SnapMessage]:
        """Join an Area (lobby channel, `u32 id`) or a room (`u32 id, u32 1, password16`)."""

        session = resolve_session(context, message, _LOGGER)
        if session is None:
            return []
        channel_type = message.type_flags & FLAG_CHANNEL_BITS
        if channel_type == FLAG_CHANNEL_BITS:
            area = self.directory.area(get_u32(message.payload, 0))
            if area is None or self._area_full(context, session, area):
                return [self._error(context, message, session, ERROR_REFUSED, channel_type)]
            departures = self._leave_room(context, session)
            context.sessions.set_lobby(session.session_id, area.area_id)
            return departures + [self._result(context, message, session, RESULT_OK, channel_type)]

        replay = self._reliable_replies.replay(message, session)
        if replay is not None:
            return replay
        room_id = get_u32(message.payload, 0)
        room = context.rooms.get(room_id)
        if room is not None and session.room_id == room_id:
            return [self._result(context, message, session, RESULT_OK, FLAG_ROOM)]
        if room is None or room.rules & ROOM_CLOSED_FLAG:
            outbound = [self._error(context, message, session, ERROR_REFUSED, FLAG_ROOM)]
        elif room.password and get_c_string(message.payload, 8) != room.password:
            outbound = [self._error(context, message, session, ERROR_WRONG_PASSWORD, FLAG_ROOM)]
        else:
            departures = self._leave_room(context, session)
            if not context.rooms.join(room_id, session.session_id):
                outbound = departures + [self._error(context, message, session, ERROR_REFUSED, FLAG_ROOM)]
            else:
                context.sessions.set_room(session.session_id, room_id)
                outbound = departures + [self._result(context, message, session, RESULT_OK, FLAG_ROOM)]
                outbound += self._room_callbacks(
                    context, room, session.session_id, commands.CMD_JOIN, self._member_record(session)
                )
        self._reliable_replies.remember(message, session, outbound)
        return outbound

    def _handle_leave(self, context: HandlerContext, message: SnapMessage) -> list[SnapMessage]:
        """Leave the room, or the Area on the lobby channel (also after a scenario)."""

        session = resolve_session(context, message, _LOGGER)
        if session is None:
            return []
        replay = self._reliable_replies.replay(message, session)
        if replay is not None:
            return replay
        channel_type = message.type_flags & FLAG_CHANNEL_BITS
        callbacks = self._leave_room(context, session)
        if channel_type == FLAG_CHANNEL_BITS:
            context.sessions.set_lobby(session.session_id, 0)
        else:
            channel_type = FLAG_ROOM
        outbound = [self._result(context, message, session, RESULT_OK, channel_type)] + callbacks
        self._reliable_replies.remember(message, session, outbound)
        return outbound

    def _handle_change_attribute(self, context: HandlerContext, message: SnapMessage) -> list[SnapMessage]:
        """Apply the host's `STAT` rules word to its room."""

        session = resolve_session(context, message, _LOGGER)
        if session is None:
            return []
        room = context.rooms.get(session.room_id)
        if room is not None and len(message.payload) == 8 and message.payload[:4] == STAT_ATTRIBUTE:
            context.rooms.set_rules(room.room_id, get_u32(message.payload, 4))
        return [self._result(context, message, session, RESULT_OK, FLAG_ROOM)]

    # Players.

    def _handle_change_user_property(self, context: HandlerContext, message: SnapMessage) -> list[SnapMessage]:
        """Store the profile and publish `sid, length, profile` to the room (slot 10, `0x00583ab0`)."""

        session = resolve_session(context, message, _LOGGER)
        if session is None:
            return []
        player = self._players.get(session.session_id)
        if player is not None and len(message.payload) == PROFILE_SIZE:
            player.profile = bytes(message.payload)
        callbacks: list[SnapMessage] = []
        room = context.rooms.get(session.room_id)
        if room is not None:
            payload = struct.pack('>2L', session.session_id, len(message.payload)) + message.payload
            callbacks = self._room_callbacks(
                context, room, session.session_id, commands.CMD_CHANGE_USER_PROPERTY, payload
            )
        return [self._result(context, message, session, RESULT_OK, FLAG_ROOM)] + callbacks

    def _handle_change_user_status(self, context: HandlerContext, message: SnapMessage) -> list[SnapMessage]:
        """Track the scenario window from the status the client reports.

        The status `1` result must succeed: the launch waits for it
        (`0x0058b1a0`, `0x005ee220`).
        """

        session = resolve_session(context, message, _LOGGER)
        if session is None:
            return []
        player = self._players.get(session.session_id)
        status = get_u32(message.payload, 0)
        if player is not None and status in (STATUS_IN_SCENARIO, STATUS_IN_LOBBY):
            player.in_scenario = status == STATUS_IN_SCENARIO
        return [self._result(context, message, session, RESULT_OK, FLAG_ROOM)]

    def _handle_search_users(self, context: HandlerContext, message: SnapMessage) -> list[SnapMessage]:
        """Answer the Friend search: `name16, u32 online, u32 Area, u32 room, u32 length, profile`.

        The client stores the first record (`0x0059a580`); online 0 shows the
        player as offline, Area 0 as outside the Areas, otherwise it looks the
        Area and room up by `OID` (`0x0058ad74`).
        """

        name = get_c_string(message.payload, 0)[:16].rstrip('\n').rstrip(SEARCH_NAME_PADDING)
        records = []
        for player in tuple(self._players.values()):
            member = context.sessions.get(player.session_id)
            if member is None or member.username != name:
                continue
            records.append(
                pack_fixed(member.username, 16)
                + struct.pack(
                    '>4L', SEARCH_ONLINE, member.lobby_id, max(member.room_id, 0), len(player.profile)
                )
                + player.profile
            )
        return [
            context.reply(
                message,
                type_flags=FLAG_CHANNEL_BITS | FLAG_RESPONSE,
                command=commands.CMD_SEARCH_USERS_BY_NAME,
                payload=struct.pack('>3L', 0, len(records), len(records)) + b''.join(records),
            )
        ]

    def _handle_search_rooms(self, context: HandlerContext, message: SnapMessage) -> list[SnapMessage]:
        """Answer the `OID` room lookup: 40-byte records, rules at `+28` (`0x0058ac20`, `0x0059a4cc`)."""

        _, conditions = parse_attribute_search(message.payload)
        records = []
        for name, operator, value in conditions:
            if name != SEARCH_OID or operator != SEARCH_EQUAL:
                continue
            room = context.rooms.get(int.from_bytes(value, 'big'))
            if room is not None:
                records.append(pack_fixed(room.name, 16) + struct.pack('>6L', 0, 0, 0, room.rules, 0, room.room_id))
        return [
            context.reply(
                message,
                type_flags=FLAG_CHANNEL_BITS | FLAG_RESPONSE,
                command=commands.CMD_SEARCH_ROOMS,
                payload=struct.pack('>3L', 0, len(records), len(records)) + b''.join(records),
            )
        ]

    # Relays.

    def _handle_send(self, context: HandlerContext, message: SnapMessage) -> list[SnapMessage]:
        """Relay chat and scenario packets with the sender's session id in the header.

        Chat (`0x0400`) goes to lobby peers only: room chat (`0xa400`) to the
        room, Area chat (`0xb400`) to the Area players outside rooms; the
        sender shows its own line (`0x0058b570`). Scenario packets go to every
        other room member unchanged (slots 18/20, `0x001cbe00`/`0x001cbea0`).
        """

        session = resolve_session(context, message, _LOGGER)
        if session is None:
            return []

        context_flags = message.type_flags & RELAY_CONTEXT_MASK
        if context_flags == TYPE_LOBBY_RELAY_REQUEST:
            ack_flags = FLAG_CHANNEL_BITS | FLAG_RESPONSE
            relay_flags = TYPE_LOBBY_RELAY | FLAG_RESPONSE
            recipients = [
                member for member in self._area_members(context, session.lobby_id)
                if member.session_id != session.session_id and member.room_id <= 0 and self._reachable(member)
            ]
        elif message.type_flags & FLAG_RELAY:
            ack_flags = FLAG_ROOM | FLAG_RESPONSE
            relay_flags = TYPE_ROOM_RELAY | FLAG_RESPONSE
            recipients = [member for member in self._room_peers(context, session) if self._reachable(member)]
        else:
            ack_flags = FLAG_ROOM | FLAG_RESPONSE
            relay_flags = message.type_flags & ~FLAG_MULTI
            recipients = self._room_peers(context, session)

        relays = [
            context.direct(
                endpoint=member.endpoint,
                session_id=member.session_id,
                type_flags=relay_flags,
                command=commands.CMD_SEND,
                payload=message.payload,
                packet_number=message.packet_number,
                acknowledge_number=ack_for_session(member),
                source_session_id=session.session_id,
            )
            for member in recipients
        ]
        # Embedded sends piggyback on the outer reliable packet, which owns the
        # only transport ACK in the bundle.
        if message.embedded_in_multi and message.type_flags & FLAG_RELIABLE:
            return relays
        return [
            context.reply(message, type_flags=ack_flags, command=commands.CMD_ACK, session_id=session.session_id)
        ] + relays

    def _handle_send_target(self, context: HandlerContext, message: SnapMessage) -> list[SnapMessage]:
        """Relay one waiting-room packet; the receiver checks the header sender id (`0x00583680`)."""

        session = resolve_session(context, message, _LOGGER)
        if session is None:
            return []
        response = [
            context.reply(message, type_flags=FLAG_ROOM | FLAG_RESPONSE, command=commands.CMD_ACK,
                          session_id=session.session_id)
        ]
        relay_payload = build_send_target_payload(message.payload)
        target = context.sessions.get(get_u32(message.payload, 4)) if relay_payload is not None else None
        if target is None or not self._reachable(target):
            _LOGGER.info('Dropping send-target from session 0x%08x: unknown target or in a scenario.', session.session_id)
            return response
        return response + [
            context.direct(
                endpoint=target.endpoint,
                session_id=target.session_id,
                type_flags=FLAG_ROOM | FLAG_RELIABLE,
                command=commands.CMD_SEND_TARGET,
                payload=relay_payload,
                acknowledge_number=ack_for_session(target),
                source_session_id=session.session_id,
            )
        ]

    # Helpers.

    def _leave_room(self, context: HandlerContext, session: Session, *, notify: bool = True) -> list[SnapMessage]:
        """Take the player out of its room and tell the lobby peers (slot 6, `u32 sid`)."""

        room = context.rooms.get(session.room_id) if session.room_id > 0 else None
        if session.room_id > 0:
            context.sessions.set_room(session.session_id, 0)
        if room is None:
            return []
        context.rooms.leave(room.room_id, session.session_id)
        if not notify:
            return []
        return build_room_leave_callbacks(
            context=context,
            leaving_session_id=session.session_id,
            recipients=[member for member in context.sessions.list_room_members(room.room_id) if self._reachable(member)],
        )

    def _room_callbacks(
        self, context: HandlerContext, room: GameRoom, exclude_session_id: int, command: int, payload: bytes
    ) -> list[SnapMessage]:
        return [
            context.direct(
                endpoint=member.endpoint,
                session_id=member.session_id,
                type_flags=FLAG_ROOM | FLAG_RESPONSE,
                command=command,
                payload=payload,
                acknowledge_number=ack_for_session(member),
            )
            for member in context.sessions.list_room_members(room.room_id)
            if member.session_id != exclude_session_id and self._reachable(member)
        ]

    def _tracking_rooms(
        self, handler: Callable[[HandlerContext, SnapMessage], list[SnapMessage]]
    ) -> Callable[[HandlerContext, SnapMessage], list[SnapMessage]]:
        """Wrap a room-changing handler with the room list updates it causes."""

        def tracked(context: HandlerContext, message: SnapMessage) -> list[SnapMessage]:
            before = self._listed_rooms(context)
            return handler(context, message) + self._room_list_updates(context, before)

        return tracked

    def _listed_rooms(self, context: HandlerContext) -> dict[int, tuple[int, bytes]]:
        """Open room rows of every Area: `room_id -> (area_id, row)`."""

        return {
            room.room_id: (area.area_id, _room_row(room))
            for area in self.directory.areas
            for room in self._open_rooms(context, area.area_id)
        }

    def _room_list_updates(self, context: HandlerContext, before: dict[int, tuple[int, bytes]]) -> list[SnapMessage]:
        """Send new or changed rows (slot 4, `0x00599f20`) and closed rooms (slot 5, `u32 id`, `0x0059a040`).

        The list keeps a started row it is told about, so closing is a delete.
        """

        after = self._listed_rooms(context)
        events = [
            (area_id, commands.CMD_CREATE_GAME_ROOM, row)
            for room_id, (area_id, row) in after.items()
            if before.get(room_id) != (area_id, row)
        ] + [
            (area_id, commands.CMD_DELETE, struct.pack('>L', room_id))
            for room_id, (area_id, _) in before.items()
            if room_id not in after
        ]
        return [
            context.direct(
                endpoint=member.endpoint,
                session_id=member.session_id,
                # Room channel: `CMD_DELETE` with the lobby flag deletes a lobby.
                type_flags=FLAG_ROOM | FLAG_RESPONSE,
                command=command,
                payload=payload,
                acknowledge_number=ack_for_session(member),
            )
            for area_id, command, payload in events
            for member in self._area_members(context, area_id)
            if self._reachable(member)
        ]

    @staticmethod
    def _open_rooms(context: HandlerContext, area_id: int) -> list[GameRoom]:
        rooms = [room for room in context.rooms.list_for_lobby(area_id) if not room.rules & ROOM_CLOSED_FLAG]
        return rooms[:MAX_LISTED_ROOMS]

    def _member_record(self, session: Session) -> bytes:
        profile = self._players[session.session_id].profile if session.session_id in self._players else bytes(PROFILE_SIZE)
        return pack_fixed(session.username, 16) + struct.pack('>2L', session.session_id, len(profile)) + profile

    def _reachable(self, session: Session) -> bool:
        """Whether lobby callbacks may reach the player (not in a scenario)."""

        player = self._players.get(session.session_id)
        return player is not None and not player.in_scenario

    @staticmethod
    def _room_peers(context: HandlerContext, session: Session) -> list[Session]:
        if session.room_id <= 0:
            return []
        return [
            member for member in context.sessions.list_room_members(session.room_id)
            if member.session_id != session.session_id
        ]

    def _area_members(self, context: HandlerContext, area_id: int) -> list[Session]:
        """Players in one Area (lobby id 0 means no Area, not an Area of its own)."""

        if self.directory.area(area_id) is None:
            return []
        return context.sessions.list_lobby_members(area_id)

    def _area_population(self, context: HandlerContext, area_id: int) -> int:
        return len(self._area_members(context, area_id))

    def _area_full(self, context: HandlerContext, session: Session, area: Area) -> bool:
        others = [
            member for member in self._area_members(context, area.area_id)
            if member.session_id != session.session_id
        ]
        return len(others) >= self.directory.capacity

    def _forget_player(self, session_id: int) -> None:
        self._players.pop(session_id, None)
        self._reliable_replies.forget(session_id)

    @staticmethod
    def _result(
        context: HandlerContext, message: SnapMessage, session: Session, status: int, channel_type: int
    ) -> SnapMessage:
        """Successful result: `selector, status`, the selector being the request command."""

        return context.reply(
            message,
            type_flags=channel_type | FLAG_RESPONSE,
            command=commands.CMD_RESULT_WRAPPER,
            payload=struct.pack('>2L', message.command, status),
            session_id=session.session_id,
            acknowledge_number=ack_for_request(message, session),
        )

    @staticmethod
    def _error(
        context: HandlerContext, message: SnapMessage, session: Session, code: int, channel_type: int
    ) -> SnapMessage:
        """Failed result: `CMD_RESULT_ERROR` with `selector, code`."""

        return context.reply(
            message,
            type_flags=channel_type | FLAG_RESPONSE,
            command=commands.CMD_RESULT_ERROR,
            payload=struct.pack('>2L', message.command, code),
            session_id=session.session_id,
            acknowledge_number=ack_for_request(message, session),
        )


def _room_row(room: GameRoom) -> bytes:
    """Room list row (`kkCreateGameRoomSwap`): name16, members, 0, rules, max players, room id."""

    return struct.pack(
        '>16s5L', pack_fixed(room.name, 16), len(room.members), 0, room.rules, room.max_players, room.room_id
    )
