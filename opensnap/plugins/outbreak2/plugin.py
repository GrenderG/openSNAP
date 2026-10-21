"""Resident Evil Outbreak File#2 game plugin.

Rules below are taken from `SLUS_209.84` (NTSC-U) and `SLES_533.19` (PAL):
the lobby overlay `netwk.bin` (`BIN/3.DAT`, loaded at NA `0x0058d000` / EU
`0x005ac300`) and the main ELF. Addresses are NA unless marked EU. The SN@P SDK
is the Monster Hunter build of File#1 (NA = File#1 v2 address `- 0x7aa68`,
EU `- 0x5e30`), so the transport findings of the other Capcom games apply.

Both regions run the same lobby code with different data addresses and title
codes (NA `0xCA19` at `0x0059d390`, EU `0xCAF3` at `0x005bd62c`); every
request and callback payload below is built identically, so NA and EU players
share one server and one set of Areas (`tasks/re_outbreak2_na_vs_eu.md`).

Scenario overlay: as in File#1, `game.bin` replaces `netwk.bin` for a scenario
while the lobby callbacks stay registered, so lobby callbacks only go to
players outside the window between status `1` (sent at `0x005a4728` right
before the scenario) and status `0xf7e00001` (sent after the callbacks are
registered again, `0x0059ce24` / `0x0059d418`). Unlike File#1, players stay
in their room after the scenario: the return path sends no room or Area leave
(`0x0059cda0`).
"""

from collections.abc import Callable
from dataclasses import dataclass, field
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
    ack_for_request,
    ack_for_session,
    build_list_pages,
    build_room_leave_callbacks,
    build_send_target_payload,
    pack_fixed,
    parse_attribute_search,
    resolve_session,
    search_text,
    send_target_relay_flags,
)
from opensnap.plugins.outbreak2.directory import Area, Directory, read_directory
from opensnap.plugins.outbreak2.records import GAME
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

_LOGGER = logging.getLogger('opensnap.plugins.outbreak2')

# `kkLoginClient` calls: 240-byte user property, also sent by `kkLoginToKICS`
# from payload `+0x128`. The account screen first runs a credential check
# login with profile size 0 (`0x00619f7c`, EU `0x00626b0c`) and logs out.
PROFILE_SIZE = 240
KICS_PROFILE_OFFSET = 0x128

# `kkChangeUserStatus` values.
STATUS_IN_SCENARIO = 1
STATUS_IN_LOBBY = 0xF7E00001

# Rooms. The roster table keeps four members (`0x005a08a0`, no bound check)
# and the room list at most 30 rows (`0x005a16f0`, limit 30), but a
# `kkSearchRooms` reply goes through a one-shot dispatcher keyed on the packet
# number (`0x005a3aa0`), so it cannot be paged: the list is one reply that fits
# the 896-byte receive buffer, 21 rows of 40 bytes. `STAT` carries
# the whole rules word; `0x40000000` marks a started or disbanded room: the
# start sends it with the room's load-bucket bits (`0x005a2748`), the disband
# alone (`0x005a05a8`). Listings skip it (`0x005a1790`) and the Friend status
# shows it as "Now playing." (`0x005b3aa0`).
MAX_ROOM_PLAYERS = 4
MAX_LISTED_ROOMS = 21
ROOM_CLOSED_FLAG = 0x40000000
STAT_ATTRIBUTE = b'STAT'
MAXI_ATTRIBUTE = b'MAXI'
# `kkSearchRooms` conditions: `LOID == area` (u32), then `STAT` with 8-byte
# values `mask, value` (type 1, operator 1: `0x21`), matched as
# `(rules & mask) == value`. The room list asks for the category bit twice and
# for `(0x40000000, 0)`, open rooms (`0x005a16f0`, tables `0x00626dd0`); the
# load counters ask for started rooms per bucket (`0x0059f394`, `0x00626e38`).
SEARCH_LOID = b'LOID'
SEARCH_STAT = b'STAT'

# Failed results (`CMD_RESULT_ERROR`, `selector, code`). The room join result
# (`0x005a0380`) shows "Incorrect password" for code 15 and a generic refusal
# for any other code; a refused Area join shows "You were unable to enter."
ERROR_WRONG_PASSWORD = 15
ERROR_REFUSED = 1
RESULT_OK = 0

# Idle limits. Scenario clients send a game packet at least every half second
# (reliable queue flush `0x002412a0`); five minutes covers the results screen
# and the APP upload before the player is back in the lobby. Lobby clients
# send an echo after 1800 idle frames and drop after 1800 more without a reply
# (`0x005b6030`, armed at login `0x0059d9e0` from APP 6213 words 6/7), which
# the shared echo handler answers; a console that went silent for five
# minutes is gone, and its seat is given back.
SCENARIO_IDLE_LIMIT_SECONDS = 5 * 60
LOBBY_IDLE_LIMIT_SECONDS = 5 * 60
# `kkSearchUsers` request: one `-` appended to names shorter than 15 (`0x005b3e58`).
SEARCH_NAME_PADDING = '-'
# Search result word `+16` is only tested for zero ("The member was not found.", `0x005b3cf0`).
SEARCH_ONLINE = 1


@dataclass(slots=True)
class _Player:
    """File#2 runtime data for one logged-in session."""

    session_id: int
    profile: bytes = bytes(PROFILE_SIZE)
    # Between status `1` and status `0xf7e00001` (see the module docstring).
    in_scenario: bool = False
    # Room members who left while this player was in a scenario: their leave
    # callbacks (slot 6) are delivered once the player is back in the lobby,
    # since the roster only drops a member on that callback.
    pending_leaves: list[int] = field(default_factory=list)


class Outbreak2Plugin(GamePlugin):
    """Command handlers for the File#2 lobby, rooms and scenarios."""

    name = GAME
    snap_titles = (
        SnapTitle(title_code=0xCA19, footer_marker=FOOTER_MARKER, name='Resident Evil Outbreak File#2 NA'),
        SnapTitle(title_code=0xCAF3, footer_marker=FOOTER_MARKER, name='Resident Evil Outbreak File#2 EU'),
    )
    companion_app = 'capcom'

    def __init__(self) -> None:
        self._players: dict[int, _Player] = {}
        self._directory: Directory | None = None

    @property
    def directory(self) -> Directory:
        assert self._directory is not None, 'register_handlers() builds the directory'
        return self._directory

    def register_handlers(self, router: CommandRouter, context: HandlerContext) -> None:
        self._directory = read_directory()
        # The Capcom APP process reports the server population (6510) from the
        # shared store; a starting game server has no players yet.
        self._online_players = context.online_players
        self._online_players.clear(GAME)
        router.register(commands.CMD_LOGIN_TO_KICS, self._tracking_rooms(self._handle_login_to_kics))
        router.register(commands.CMD_LOGOUT_CLIENT, self._tracking_rooms(self._handle_logout))
        router.register(commands.CMD_QUERY_AREA, self._handle_query_area)
        router.register(commands.CMD_QUERY_ATTRIBUTE, self._handle_query_attribute)
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
        messages = self._leave_room(context, session) + self._leave_area(context, session)
        self._forget_player(session.session_id)
        return messages + self._room_list_updates(context, before)

    def session_idle_limit(self, context: HandlerContext, session: Session) -> float | None:
        player = self._players.get(session.session_id)
        if player is None:
            return None
        return SCENARIO_IDLE_LIMIT_SECONDS if player.in_scenario else LOBBY_IDLE_LIMIT_SECONDS

    # Login.

    def _handle_login_to_kics(self, context: HandlerContext, message: SnapMessage) -> list[SnapMessage]:
        handoff = game_handlers.find_handoff(context, message)
        previous = None if handoff is None else context.sessions.get(handoff.session_id)
        if previous is not None:
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
        session = context.sessions.get(session_id)
        if session is not None:
            self._online_players.login(GAME, session)
        return responses

    def _handle_logout(self, context: HandlerContext, message: SnapMessage) -> list[SnapMessage]:
        session = resolve_session(context, message, _LOGGER)
        if session is None:
            return []
        messages = self._leave_room(context, session) + self._leave_area(context, session)
        self._forget_player(session.session_id)
        return messages

    # Areas.

    def _handle_query_area(self, context: HandlerContext, message: SnapMessage) -> list[SnapMessage]:
        """List Areas by `NAME` range or `OID`.

        Rows (`0x005a0fe0`, `0x005a3ca0`): name16, users, then three words the
        client stores but never reads, then the Area id. The login sums the
        users of every row against the server capacity (`0x0059c730`).
        """

        limit, conditions = parse_attribute_search(message.payload)
        areas: tuple[Area, ...] = ()
        low = high = None
        for name, operator, value in conditions:
            if name == SEARCH_OID and operator == SEARCH_EQUAL:
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
        """Answer `USER` (members) or `MAXI` (capacity) of a room, or `USER` of an Area.

        Reply `id, name, value`; the client checks the name it asked for
        (room `0x0059fe20`, Area `0x0059f4f0`). Areas are only asked `USER`.
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

    def _handle_search_rooms(self, context: HandlerContext, message: SnapMessage) -> list[SnapMessage]:
        """Answer every `kkSearchRooms` of the lobby: `LOID` + masked `STAT`, or `OID`.

        Records are 40 bytes (`0x005a1790`, `0x005a3d20`): name16, 0, users
        `+20`, 0, rules `+28`, max players `+32`, room id `+36`. At most
        `MAX_LISTED_ROOMS` rows fit one reply; the lobby skips rows with
        `0x40000000`; the load
        counters sum the users of started rooms per bucket, so closed rooms
        are reported when the conditions ask for them.
        """

        limit, conditions = parse_attribute_search(message.payload)
        rooms: list[GameRoom] | None = None
        masks: list[tuple[int, int]] = []
        for name, operator, value in conditions:
            if name == SEARCH_OID and operator == SEARCH_EQUAL:
                room = context.rooms.get(int.from_bytes(value, 'big'))
                rooms = [] if room is None else [room]
            elif name == SEARCH_LOID and operator == SEARCH_EQUAL:
                area_id = int.from_bytes(value, 'big')
                rooms = context.rooms.list_for_lobby(area_id) if self.directory.area(area_id) else []
            elif name == SEARCH_STAT and operator == SEARCH_EQUAL and len(value) == 8:
                masks.append(struct.unpack('>2L', value))
        records = [
            _search_record(room)
            for room in (rooms or [])
            if all((room.rules & mask) == expected for mask, expected in masks)
        ][:min(limit or MAX_LISTED_ROOMS, MAX_LISTED_ROOMS)]
        return [
            context.reply(
                message,
                type_flags=FLAG_CHANNEL_BITS | FLAG_RESPONSE,
                command=commands.CMD_SEARCH_ROOMS,
                payload=struct.pack('>3L', 0, len(records), len(records)) + b''.join(records),
            )
        ]

    def _handle_query_user(self, context: HandlerContext, message: SnapMessage) -> list[SnapMessage]:
        """Return a room's roster: `name16, sid, length, profile` per member (`0x005a08a0`).

        A room-channel query for room id 0 is the client asking for its own
        room. The game sends it when it returns from a scenario: the roster
        caller `0x005a2150` reads the room id from the global `0x008a5920`,
        which is 0 after a scenario, so the meeting room's member count comes
        from this reply. Resolve id 0 to the caller's room so it shows the real
        count instead of "0P". The reply's room-id field is not read by the
        parser (count at `+8`, records at `+12`), so it echoes the request.
        """

        channel_type = message.type_flags & FLAG_CHANNEL_BITS
        requested_id = get_u32(message.payload, 0)
        room_id = requested_id
        if channel_type == FLAG_ROOM and requested_id == 0:
            session = resolve_session(context, message, _LOGGER)
            room_id = 0 if session is None else session.room_id
        room = context.rooms.get(room_id) if channel_type == FLAG_ROOM else None
        records = []
        for member_id in [] if room is None else sorted(room.members)[:MAX_ROOM_PLAYERS]:
            member = context.sessions.get(member_id)
            if member is not None:
                records.append(self._member_record(member))
        # A full room's roster (4 x 264 bytes) exceeds the client's receive
        # buffer; the roster callbacks count records across pages.
        return build_list_pages(
            context,
            message,
            type_flags=channel_type | FLAG_RESPONSE,
            command=commands.CMD_QUERY_USER,
            header=lambda count: struct.pack('>3L', requested_id, len(records), count),
            records=records,
        )

    def _handle_create_game_room(self, context: HandlerContext, message: SnapMessage) -> list[SnapMessage]:
        """Create a room in the player's Area.

        Request (44 bytes, `0x005a35f0`): name16, `+16` max players, `+20`
        password16, `+36` 1, `+40` rules. The result carries the room id
        (`selector, id`, `0x005a3740`).
        """

        session = resolve_session(context, message, _LOGGER)
        if session is None:
            return []

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
        return outbound

    def _handle_join(self, context: HandlerContext, message: SnapMessage) -> list[SnapMessage]:
        """Join an Area (lobby channel, `u32 id`, `0x0059f8c4`) or a room (`u32 id, u32 1, password16`, `0x005a0338`)."""

        session = resolve_session(context, message, _LOGGER)
        if session is None:
            return []
        channel_type = message.type_flags & FLAG_CHANNEL_BITS
        if channel_type == FLAG_CHANNEL_BITS:
            area = self.directory.area(get_u32(message.payload, 0))
            if area is None or self._server_full(context, session):
                return [self._error(context, message, session, ERROR_REFUSED, channel_type)]
            departures = self._leave_room(context, session) + self._leave_area(context, session)
            self._set_area(context, session, area.area_id)
            arrival = self._area_callbacks(context, session, commands.CMD_JOIN)
            return departures + [self._result(context, message, session, RESULT_OK, channel_type)] + arrival

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
                    context,
                    room,
                    session.session_id,
                    commands.CMD_JOIN,
                    self._member_record(session),
                    type_flags=FLAG_ROOM | FLAG_RESPONSE | FLAG_RELIABLE,
                )
        return outbound

    def _handle_leave(self, context: HandlerContext, message: SnapMessage) -> list[SnapMessage]:
        """Leave the room (`0x005a0510`), or the Area on the lobby channel (`0x0059fc18`)."""

        session = resolve_session(context, message, _LOGGER)
        if session is None:
            return []
        channel_type = message.type_flags & FLAG_CHANNEL_BITS
        callbacks = self._leave_room(context, session)
        if channel_type == FLAG_CHANNEL_BITS:
            callbacks += self._leave_area(context, session)
        else:
            channel_type = FLAG_ROOM
        outbound = [self._result(context, message, session, RESULT_OK, channel_type)] + callbacks
        return outbound

    def _handle_change_attribute(self, context: HandlerContext, message: SnapMessage) -> list[SnapMessage]:
        """Apply the host's `STAT` word to its room (start `0x005a2748`, disband `0x005a05a8`)."""

        session = resolve_session(context, message, _LOGGER)
        if session is None:
            return []
        room = context.rooms.get(session.room_id)
        if room is not None and len(message.payload) == 8 and message.payload[:4] == STAT_ATTRIBUTE:
            context.rooms.set_rules(room.room_id, get_u32(message.payload, 4))
        return [self._result(context, message, session, RESULT_OK, FLAG_ROOM)]

    # Players.

    def _handle_change_user_property(self, context: HandlerContext, message: SnapMessage) -> list[SnapMessage]:
        """Store the profile and publish `sid, length, profile` to the room (slot 10, `0x0059c1d0`)."""

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
                context,
                room,
                session.session_id,
                commands.CMD_CHANGE_USER_PROPERTY,
                payload,
                type_flags=FLAG_ROOM | FLAG_RESPONSE,
            )
        return [self._result(context, message, session, RESULT_OK, FLAG_ROOM)] + callbacks

    def _handle_change_user_status(self, context: HandlerContext, message: SnapMessage) -> list[SnapMessage]:
        """Track the scenario window from the status the client reports.

        The status `1` result must succeed: the launch waits for it
        (`0x005a4420`).
        """

        session = resolve_session(context, message, _LOGGER)
        if session is None:
            return []
        player = self._players.get(session.session_id)
        status = get_u32(message.payload, 0)
        deferred: list[SnapMessage] = []
        if player is not None and status in (STATUS_IN_SCENARIO, STATUS_IN_LOBBY):
            player.in_scenario = status == STATUS_IN_SCENARIO
            if not player.in_scenario:
                deferred = [
                    message_
                    for left in player.pending_leaves
                    for message_ in build_room_leave_callbacks(
                        context=context,
                        leaving_session_id=left,
                        recipients=[session],
                        type_flags=FLAG_ROOM | FLAG_RESPONSE | FLAG_RELIABLE,
                    )
                ]
                player.pending_leaves.clear()
        return [self._result(context, message, session, RESULT_OK, FLAG_ROOM)] + deferred

    def _handle_search_users(self, context: HandlerContext, message: SnapMessage) -> list[SnapMessage]:
        """Answer the Friend search: `name16, u32 online, u32 Area, u32 room, u32 length, profile`.

        The client stores the first record (`0x005b3cf0`); online 0 shows the
        player as not found, Area 0 as "AREA SELECT", otherwise it looks the
        Area and room up by `OID` (`0x005a3da0`).
        """

        name = get_c_string(message.payload, 0)[:16].rstrip(SEARCH_NAME_PADDING)
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

    # Relays.

    def _handle_send(self, context: HandlerContext, message: SnapMessage) -> list[SnapMessage]:
        """Relay chat and scenario packets with the sender's session id in the header.

        Chat (`0x0400`) goes to lobby peers only, and the sender shows its own
        line (`0x005a47f0`). Area chat (`0xb400`) is the lobby's normal
        channel, also for players in quick-start rooms, and carries the Area
        join announces that compare the clients' APP data tokens
        (`0x005a4ac0`, `0x0059c3d4`); room chat (`0xa400`) is used inside
        titled rooms (`0x005a4968`). Scenario packets go to every other room
        member unchanged (slots 18/20, `0x001f77f0`/`0x001f7890`).
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
                if member.session_id != session.session_id and self._reachable(member)
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
        """Relay one waiting-room packet; the receiver checks the header sender id (`0x0059b7a0`)."""

        session = resolve_session(context, message, _LOGGER)
        if session is None:
            return []
        # A bundle's reliable children share its one sequence: ACKing a child as
        # 0 can free an unsent client message (as in File#1, `0x001e7968..0x001e7a20`).
        response = [] if message.embedded_in_multi and message.type_flags & FLAG_RELIABLE else [
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
                type_flags=send_target_relay_flags(message.type_flags),
                command=commands.CMD_SEND_TARGET,
                payload=relay_payload,
                acknowledge_number=ack_for_session(target),
                source_session_id=session.session_id,
            )
        ]

    # Helpers.

    def _leave_room(self, context: HandlerContext, session: Session, *, notify: bool = True) -> list[SnapMessage]:
        """Take the player out of its room and tell the lobby peers (slot 6, `u32 sid`, `0x0059b3f0`)."""

        room = context.rooms.get(session.room_id) if session.room_id > 0 else None
        if session.room_id > 0:
            context.sessions.set_room(session.session_id, 0)
        if room is None:
            return []
        context.rooms.leave(room.room_id, session.session_id)
        if not notify:
            return []
        recipients = []
        for member in context.sessions.list_room_members(room.room_id):
            if self._reachable(member):
                recipients.append(member)
            elif member.session_id in self._players:
                self._players[member.session_id].pending_leaves.append(session.session_id)
        return build_room_leave_callbacks(
            context=context,
            leaving_session_id=session.session_id,
            recipients=recipients,
            type_flags=FLAG_ROOM | FLAG_RESPONSE | FLAG_RELIABLE,
        )

    def _leave_area(self, context: HandlerContext, session: Session) -> list[SnapMessage]:
        """Take the player out of its Area and tell the Area peers (slot 7)."""

        if self.directory.area(session.lobby_id) is None:
            return []
        callbacks = self._area_callbacks(context, session, commands.CMD_LEAVE)
        self._set_area(context, session, 0)
        return callbacks

    def _area_callbacks(self, context: HandlerContext, session: Session, command: int) -> list[SnapMessage]:
        """Area join (slot 3) or leave (slot 7) callbacks: `u32 sid` on the lobby channel.

        The handlers (`0x0059b660`, `0x0059b700`) read no payload; they count
        the viewer's Area up or down, which is how File#2 keeps the Area user
        counts current between Area queries.
        """

        return [
            context.direct(
                endpoint=member.endpoint,
                session_id=member.session_id,
                type_flags=FLAG_CHANNEL_BITS | FLAG_RESPONSE,
                command=command,
                payload=struct.pack('>L', session.session_id),
                acknowledge_number=ack_for_session(member),
            )
            for member in self._area_members(context, session.lobby_id)
            if member.session_id != session.session_id and self._reachable(member)
        ]

    def _room_callbacks(
        self,
        context: HandlerContext,
        room: GameRoom,
        exclude_session_id: int,
        command: int,
        payload: bytes,
        type_flags: int,
    ) -> list[SnapMessage]:
        """Room callbacks to the reachable members; joins and leaves are reliable.

        The member list gains a player only from the join callback (slot 2
        `0x0059b250` -> event 31 `0x00592a40`) and loses one only on the leave
        callback (slot 6 `0x0059b3f0` -> event 32 `0x00592ca0`); it is rebuilt
        only on room entry and before a scenario (`0x005ec1a0`, `0x005ed790`).
        The SDK drops an unreliable packet that is lost or arrives after a
        newer one (`0x00161e64..0x00161e98`), so one lost callback leaves a
        player unseen, or a stale one, in the room. A reliable one is ACKed on
        receipt (`0x00161e14`), delivered in order, and dispatched to the same
        slot (`kkDispatchingOperation` `0x00165340` never reads
        `0x8000`/`0x4000`); leaves are reliable too, so that one cannot
        overtake a join held behind a gap.
        """

        return [
            context.direct(
                endpoint=member.endpoint,
                session_id=member.session_id,
                type_flags=type_flags,
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
        """Send new or changed rows (slot 4, `0x005b3800`) and closed rooms (slot 5, `u32 id`, `0x005b3930`).

        Slot 4 only acts on titled rooms (rules bit `0x80000`) and keeps a
        started row it is told about, so closing is a delete.
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

    def _server_full(self, context: HandlerContext, session: Session) -> bool:
        """Whether the Areas together hold the server capacity, not counting `session` itself."""

        others = sum(
            1
            for area in self.directory.areas
            for member in self._area_members(context, area.area_id)
            if member.session_id != session.session_id
        )
        return others >= self.directory.capacity

    def _set_area(self, context: HandlerContext, session: Session, area_id: int) -> None:
        context.sessions.set_lobby(session.session_id, area_id)
        self._online_players.set_area(session.session_id, area_id)

    def _forget_player(self, session_id: int) -> None:
        self._players.pop(session_id, None)
        self._online_players.logout(session_id)

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
    """Slot 4 row (36 bytes, `0x005b3800`): name16, members, 0, rules, max players, room id."""

    return struct.pack(
        '>16s5L', pack_fixed(room.name, 16), len(room.members), 0, room.rules, room.max_players, room.room_id
    )


def _search_record(room: GameRoom) -> bytes:
    """`kkSearchRooms` record (40 bytes, `0x005a1790`): name16, 0, members, 0, rules, max players, room id."""

    return pack_fixed(room.name, 16) + struct.pack(
        '>6L', 0, len(room.members), 0, room.rules, room.max_players, room.room_id
    )
