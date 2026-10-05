"""Monster Hunter game plugin.

Lobby behavior follows the shared SNAP room model used by Auto Modellista.
Monster Hunter specific rules below are taken from the `SLUS_208.96` lobby
overlay (`lobby.bin`, load address `0x005be900`) and are cited inline.
"""

from collections.abc import Callable
from dataclasses import dataclass, replace
import logging
import struct

from opensnap.core.context import HandlerContext
from opensnap.core.game import handlers as game_handlers
from opensnap.core.rooms import GameRoom
from opensnap.core.router import CommandRouter
from opensnap.core.sessions import Session
from opensnap.plugins.base import GamePlugin, SnapTitle
from opensnap.plugins.common import (
    USER_ATTRIBUTE_TOKEN,
    ack_for_request,
    ack_for_session,
    build_room_leave_callbacks,
    build_send_target_payload,
    pack_fixed,
    resolve_session,
)
from opensnap.plugins.monsterhunter.app import AppFlowTracker, AppService, read_app_service_config
from opensnap.plugins.monsterhunter.directory import TOWNS_PER_CATEGORY, Area, Directory, Land, read_directory
from opensnap.plugins.monsterhunter.profile import MONSTER_HUNTER_NA, MonsterHunterProfile
from opensnap.protocol import GameTags, commands
from opensnap.protocol.constants import (
    FLAG_CHANNEL_BITS,
    FLAG_MULTI,
    FLAG_RELIABLE,
    FLAG_RESPONSE,
    FLAG_ROOM,
    FOOTER_MARKER,
    RELAY_CONTEXT_MASK,
    RESULT_WRAPPER_STATUS_ERROR_DIALOG,
    RESULT_WRAPPER_STATUS_OK,
    TYPE_LOBBY_RELAY,
    TYPE_LOBBY_RELAY_REQUEST,
    TYPE_ROOM_RELAY,
)
from opensnap.protocol.fields import get_c_string, get_u32
from opensnap.protocol.models import SnapMessage

_LOGGER = logging.getLogger('opensnap.plugins.monsterhunter')

# Room rules words.
# - Town rooms are created as `(town_number << 8) | 4` (`0x00615cd8`), and the
#   Town directory callback only lists rows with bit `0x4` set (`0x00617400`).
# - Quest rooms are created with rules `2` (`0x00611b44`).
# - The quest host marks departure with `STAT 0x40000002` (`0x0061ca8c`); the
#   directory skips bit `0x40000000` rows (`0x006173f4`). From then on the
#   members run `game.bin`, which replaces `lobby.bin` at the same address and
#   registers no SNAP callbacks, so lobby callbacks (`CMD_JOIN`, `CMD_LEAVE`,
#   `CMD_CHANGE_USER_PROPERTY`) must not be sent to them.
TOWN_RULES_FLAG = 0x00000004
QUEST_STARTED_RULES_FLAG = 0x40000000
STAT_ATTRIBUTE = b'STAT'

# Town directory events. Besides its one `CMD_QUERY_GAME_ROOMS` request
# (`0x00617280`), the lobby keeps its Town table current from two callbacks:
# slot 4 (`CMD_CREATE_GAME_ROOM`, `0x006101b0`) adds the 36-byte room row to
# its category or refreshes the row with the same room id, skipping rows with
# rules bit `0x2` or `0x40000000`; slot 5 (`CMD_DELETE` without the lobby flag
# `0x1000`, `0x00610370`) removes the row with that room id. SDK dispatch:
# `kkDispatchingOperation` (Beta1 `0x002e7d88`, `0x002e7da8`).

# Member profile (`CMD_CHANGE_USER_PROPERTY` payload, slot 10 `0x0061a0b0`).
PROFILE_SIZE = 140
# Quest departure. A player leaving its Town for a quest stays listed there.
# The departure is announced differently by the two sides (`lobby.bin`):
# - Guests publish profile `+136/+137 = 3/5` (`0x0061cb64`, step 0 of the
#   guest machine `0x0061c7a0`) before their room leave (step 4, `0x0061cdd0`).
# - The host, solo or with a party, starts with selector 3 to every party
#   member, itself first (`0x00611210`: its own entry goes to slot 0, then
#   `0x00610520` per member: `u32 1, u32 target, 8-byte envelope` with byte 2
#   = 3) in phase 0 of its departure machine (`0x00610f30`). Phase 0 has no
#   abort (its waits time out forward, `0x00611490`) and hands over to phase 1
#   (`0x006119a0`), whose step 0 is the room leave (`0x00611a80`), then the
#   quest room `CREATE` (step 2) and only then its 3/5 profile (`0x00611d1c`,
#   step 5). The machine is only deactivated on completion (`0x0061116c`), so
#   the host's next room change after selector 3 is always this leave.
PROFILE_PARTY_STATE_OFFSET = 136
PROFILE_QUEST_STATE_OFFSET = 137
PROFILE_DEPARTING_STATE = (3, 5)
DEPARTURE_PREPARATION_SELECTOR = 3
DEPARTURE_PREPARATION_SIZE = 16

# Town categories are rules bits 24-26 (`0x006173a0`).
TOWN_CATEGORY_SHIFT = 24
TOWN_CATEGORY_MASK = 0x7

# Idle limits (see `MonsterHunterPlugin.session_idle_limit`).
LOBBY_IDLE_LIMIT_SECONDS = 5 * 60
QUEST_IDLE_LIMIT_SECONDS = (50 + 10) * 60
# `kkSearchUsers` request (`0x00207d6c`): name16 padded with `-`.
SEARCH_NAME_PADDING = '-'
# Attribute searches (`CMD_QUERY_AREA` `0x0020873c`, `CMD_SEARCH_ROOMS`
# `0x00208cc8`): `u32 limit, u8 count`, padded to 8 bytes, then conditions
# `u32 name, u8 type << 5 | operator, value` with 4/8/16-byte values for
# types 0/1/2. Observed operators: 1 equal (`OID`, `0x00613a8c`), 4 at least
# and 6 at most (`NAME`, `0x00612950`).
SEARCH_HEADER_SIZE = 8
SEARCH_VALUE_SIZES = {0: 4, 1: 8, 2: 16}
SEARCH_EQUAL = 1
SEARCH_AT_LEAST = 4
SEARCH_AT_MOST = 6
SEARCH_OID = b'OID\x00'
SEARCH_NAME = b'NAME'


@dataclass(slots=True)
class _Player:
    """Monster Hunter runtime data for one logged-in session."""

    session_id: int
    profile: bytes | None = None
    # Area joined on the lobby channel.
    area_id: int | None = None
    # After a quest the client keeps addressing its quest room id while the
    # server has moved it back to the origin Town: `(client_id, server_id)`.
    room_alias: tuple[int, int] | None = None
    # Quest host between its departure preparation and its next room change.
    preparing_departure: bool = False


class MonsterHunterPlugin(GamePlugin):
    """Command handlers for Monster Hunter Town and quest flow."""

    name = MONSTER_HUNTER_NA.identifier
    # NA `SLUS_208.96` (`0x0024a248`) and EU `SLES_527.07` (`0x0023a528`) both
    # route here, towards one cross-region server.
    snap_titles = (
        SnapTitle(title_code=0xCA03, footer_marker=FOOTER_MARKER, name='Monster Hunter NA'),
        SnapTitle(title_code=0xCA0E, footer_marker=FOOTER_MARKER, name='Monster Hunter EU'),
    )

    def __init__(self, profile: MonsterHunterProfile = MONSTER_HUNTER_NA) -> None:
        self.profile = profile
        self.name = profile.identifier
        self._players: dict[int, _Player] = {}
        self._app_flows = AppFlowTracker()
        self._app_service: AppService | None = None
        self._directory: Directory | None = None
        # Exact reliable retries replay the requester's original result:
        # `(session, command, channel)` -> (sequence, result).
        #
        # Transport (`SLUS_208.96` receive `0x001fe778`, which merges AM's
        # `kkDispatchingPacket` and `kkReceiveExtentCheck`): ACKs are taken from
        # response-flagged packets (`kkSetRevAck` `0x002098b8`), reliable
        # duplicates below the window are dropped, multi datagrams hold up to
        # 56 entries, as in AM. Unlike AM (`app+16 = seq + 1`), an accepted
        # unreliable packet sets `app+16 = seq`, so MH accepts a repeat of its
        # newest unreliable sequence. Callbacks to other hunters are therefore
        # not replayed: they travel on links the requester's retry says nothing
        # about, and a second copy is applied again (room join `0x00619430`
        # shows the arrival again and counts the member twice, leave
        # `0x00619590` likewise). The requester's own result callbacks only set
        # the result flag and room id again (`0x00615f50`, `0x00616010`).
        self._reliable_replies: dict[tuple[int, int, int], tuple[int, tuple[SnapMessage, ...]]] = {}

    def register_handlers(self, router: CommandRouter, context: HandlerContext) -> None:
        """Register plugin handlers."""

        self._directory = read_directory(max_players_per_town=context.config.server.max_players_per_room)
        router.register(commands.CMD_LOGIN_TO_KICS, self._tracking_towns(self._handle_login_to_kics))
        router.register(commands.CMD_LOGOUT_CLIENT, self._tracking_towns(self._handle_logout))
        router.register(commands.CMD_QUERY_AREA, self._handle_query_area)
        router.register(commands.CMD_QUERY_ATTRIBUTE, self._handle_query_attribute)
        router.register(commands.CMD_QUERY_GAME_ROOMS, self._handle_query_game_rooms)
        router.register(commands.CMD_QUERY_USER, self._handle_query_user)
        router.register(commands.CMD_SEARCH_USERS_BY_NAME, self._handle_search_users)
        router.register(commands.CMD_SEARCH_ROOMS, self._handle_search_rooms)
        router.register(commands.CMD_CREATE_GAME_ROOM, self._tracking_towns(self._handle_create_game_room))
        router.register(commands.CMD_JOIN, self._tracking_towns(self._handle_join))
        router.register(commands.CMD_LEAVE, self._tracking_towns(self._handle_leave))
        router.register(commands.CMD_SEND_ECHO, self._lobby_keepalive(game_handlers.handle_send_echo))
        router.register(commands.CMD_SEND, self._handle_send)
        router.register(commands.CMD_SEND_TARGET, self._handle_send_target)
        router.register(commands.CMD_CHANGE_USER_STATUS, self._handle_change_user_status)
        router.register(
            commands.CMD_CHANGE_USER_PROPERTY, self._lobby_keepalive(self._handle_change_user_property)
        )
        router.register(commands.CMD_CHANGE_ATTRIBUTE, self._tracking_towns(self._handle_change_attribute))

    def _lobby_keepalive(
        self, handler: Callable[[HandlerContext, SnapMessage], list[SnapMessage]]
    ) -> Callable[[HandlerContext, SnapMessage], list[SnapMessage]]:
        """Send no reply callback for a lobby keepalive from a started quest.

        `lobby.bin` keeps its session alive with a profile update plus a
        64-byte echo, and registers each reply callback with its request: the
        echo reply runs slot 40 (`kkSendEchoPacket` `0x00207b90` ->
        `0x00207d30`; callers `0x006135c0`, `0x0061ab50`), the property result
        slot 38 (result dispatch `0x00202e10`, `0x002063f4`; callers
        `0x00611e00`, `0x00617bf0`, `0x0061b6c0`, `0x0061cfc0`). When the quest
        starts, `game.bin` overwrites `lobby.bin` (it makes no SNAP requests and
        registers no callbacks) and the slots keep pointing into it, so the
        reply to a keepalive still in flight jumps into the quest overlay and
        hangs the client at "Preparing Quest". The profile is still stored.
        Once the return `STAT` moves the player back to its Town, keepalives
        are answered again. Transport ACKs only retire packets (`kkSetRevAck`).
        """

        def handle(context: HandlerContext, message: SnapMessage) -> list[SnapMessage]:
            requester = context.sessions.get(message.session_id)
            in_quest = requester is not None and self._in_started_quest(context, requester)
            outbound = handler(context, message)
            if not in_quest:
                return outbound
            return [
                reply for reply in outbound
                if reply.session_id != requester.session_id
                or reply.command not in (commands.CMD_RESULT_WRAPPER, commands.CMD_SEND_ECHO)
            ]

        return handle

    def start_services(self, context: HandlerContext) -> None:
        """Start the APP TCP service that the client uses beside SNAP."""

        self._app_service = AppService(
            config=read_app_service_config(context.config, self.profile),
            flows=self._app_flows,
            directory=self.directory,
            land_population=self._land_population,
            records=context.records,
            session=context.sessions.get,
        )
        self._app_service.start()

    def stop_services(self) -> None:
        """Stop the APP TCP service."""

        if self._app_service is not None:
            self._app_service.stop()
            self._app_service = None

    def on_session_timeout(self, context: HandlerContext, session: Session) -> list[SnapMessage]:
        """Remove one timed-out player from Land, Area and rooms."""

        before = self._listed_towns(context)
        messages = self._detach(context, session, keep_town_listing=False)
        self._forget_player(session.session_id)
        return messages + self._town_directory_updates(context, before)

    def session_idle_limit(self, context: HandlerContext, session: Session) -> float | None:
        """End a crashed player's session once its client has gone silent.

        In an Area or Town the client sends a profile keepalive every 30 s
        (longer gaps, up to about 2 minutes, while its browser is open). A
        started quest runs in `game.bin`, which sends nothing until the quest
        ends (at most 50 minutes in any quest file). On World and Land
        selection the client talks only to the APP service, so no limit
        applies there.
        """

        if self._in_started_quest(context, session):
            return QUEST_IDLE_LIMIT_SECONDS
        player = self._players.get(session.session_id)
        if (player is not None and player.area_id is not None) or session.room_id > 0:
            return LOBBY_IDLE_LIMIT_SECONDS
        return None

    def _handle_login_to_kics(self, context: HandlerContext, message: SnapMessage) -> list[SnapMessage]:
        """Admit the player to the Land after the shared KICS login."""

        handoff = context.handoffs.get(message.session_id) or context.handoffs.get_by_endpoint(message.endpoint)
        previous = context.sessions.get(message.session_id) or context.sessions.get_by_endpoint(message.endpoint)
        if previous is not None and handoff is not None and previous.session_id == handoff.session_id:
            # The shared handler silently clears room state on a repeated
            # login; also drop Monster Hunter Town listings. A different
            # (superseded) session is ended by the shared handler instead.
            self._detach(context, previous, keep_town_listing=False, notify=False)

        responses = game_handlers.handle_login_to_kics(context, message)
        if not responses:
            return responses

        session = context.sessions.get(responses[0].session_id)
        if session is not None:
            # KICS repeats during Land entry; keep the player and its APP phase.
            self._players.setdefault(session.session_id, _Player(session_id=session.session_id))
            self._app_flows.arm_world(session.session_id, session.endpoint.host)
        return responses

    def _handle_logout(self, context: HandlerContext, message: SnapMessage) -> list[SnapMessage]:
        """Handle `kkLogout`: no wire reply, matching the shared core handler."""

        session = resolve_session(context, message, _LOGGER)
        if session is not None:
            self._detach(context, session, keep_town_listing=False, notify=False)
            self._forget_player(session.session_id)
        return []

    @property
    def directory(self) -> Directory:
        assert self._directory is not None, 'register_handlers() builds the directory'
        return self._directory

    def _handle_query_area(self, context: HandlerContext, message: SnapMessage) -> list[SnapMessage]:
        """List one Land's Areas (`NAME` range `<key>01..<key>26`).

        Records use the room-list layout read by `0x00612c20`: name, users,
        capacity, two unused words, Area id.
        """

        session = resolve_session(context, message, _LOGGER)
        if session is None:
            return []

        limit, conditions = _parse_search(message.payload)
        low = next((value for name, op, value in conditions if name == SEARCH_NAME and op == SEARCH_AT_LEAST), None)
        high = next((value for name, op, value in conditions if name == SEARCH_NAME and op == SEARCH_AT_MOST), None)
        areas = () if low is None or high is None else self.directory.areas_in_name_range(
            _search_text(low), _search_text(high)
        )
        entries = [
            struct.pack(
                '>16s5L',
                pack_fixed(area.name, 16),
                self._area_population(area.area_id),
                self.directory.area_capacity,
                0,
                0,
                area.area_id,
            )
            for area in areas[:limit]
        ]
        return [
            context.reply(
                message,
                type_flags=FLAG_CHANNEL_BITS | FLAG_RESPONSE,
                command=commands.CMD_QUERY_AREA,
                payload=struct.pack('>3L', limit, 1, len(entries)) + b''.join(entries),
            )
        ]

    def _handle_query_attribute(self, context: HandlerContext, message: SnapMessage) -> list[SnapMessage]:
        """Report `USER` counts for one Area or one room."""

        session = resolve_session(context, message, _LOGGER)
        if session is None:
            return []

        channel_type = message.type_flags & FLAG_CHANNEL_BITS
        channel_id = get_u32(message.payload, 0)
        if channel_type == FLAG_ROOM:
            room = context.rooms.get(self._server_room_id(session, channel_id))
            users = 0 if room is None else len(room.members)
        else:
            users = self._area_population(channel_id)

        return [
            context.reply(
                message,
                type_flags=channel_type | FLAG_RESPONSE,
                command=commands.CMD_QUERY_ATTRIBUTE,
                payload=struct.pack('>L4sL', channel_id, USER_ATTRIBUTE_TOKEN, users),
            )
        ]

    def _handle_query_game_rooms(self, context: HandlerContext, message: SnapMessage) -> list[SnapMessage]:
        """List every room of one Area; the client keeps only open Towns."""

        area_id = get_u32(message.payload, 0)
        entries = [_room_row(context, room) for room in context.rooms.list_for_lobby(area_id)]
        return [
            context.reply(
                message,
                type_flags=FLAG_CHANNEL_BITS | FLAG_RESPONSE,
                command=commands.CMD_QUERY_GAME_ROOMS,
                payload=struct.pack('>3L', 0, 1, len(entries)) + b''.join(entries),
            )
        ]

    def _handle_query_user(self, context: HandlerContext, message: SnapMessage) -> list[SnapMessage]:
        """Return the member roster of one room or one Area.

        Both callbacks (room `0x006182f0`, Area `0x00614060`) read
        `name16, session_id, length` and always copy a 140-byte profile from
        `+24`, so only members that already published their profile are listed.
        """

        session = resolve_session(context, message, _LOGGER)
        if session is None:
            return []

        channel_type = message.type_flags & FLAG_CHANNEL_BITS
        requested_id = get_u32(message.payload, 0)
        if channel_type == FLAG_ROOM:
            room = context.rooms.get(self._server_room_id(session, requested_id))
            member_ids = () if room is None else sorted(room.members)
        else:
            # The Area roster feeds the hunter search, whose results can be
            # registered as Friends; its filter (`0x00613f10`) never skips the
            # searcher, and a Friend entry for oneself crashes the game later.
            member_ids = sorted(
                player.session_id for player in tuple(self._players.values())
                if player.area_id == requested_id and player.session_id != session.session_id
            )

        entries = []
        for member_id in member_ids:
            member = context.sessions.get(member_id)
            profile = self._profile(member_id)
            if member is not None and profile is not None:
                entries.append(_member_record(member, profile))

        payload = struct.pack('>3L', requested_id, len(entries), len(entries)) + b''.join(entries)
        return [
            context.reply(
                message,
                type_flags=channel_type | FLAG_RESPONSE,
                command=commands.CMD_QUERY_USER,
                payload=payload,
            )
        ]

    def _handle_search_users(self, context: HandlerContext, message: SnapMessage) -> list[SnapMessage]:
        """Answer the Friend search with the member's Land, Area and room.

        Result records (`SearchUsersSwap`): `name16, u32 land, u32 area,
        u32 room, u32 length, data`. The Friend status (`0x006122e0`,
        `0x00612050`) shows "not found" for land `0`, Land selection for area
        `0`, and resolves the room through `CMD_SEARCH_ROOMS`.
        """

        session = resolve_session(context, message, _LOGGER)
        name = get_c_string(message.payload, 0)[:16].rstrip('\n').rstrip(SEARCH_NAME_PADDING)
        records = []
        for player in tuple(self._players.values()):
            member = context.sessions.get(player.session_id)
            if member is None or member.username != name:
                continue
            # Never report the searcher: a Friend entry for oneself shows as
            # "not found" instead of leading into the player's own room.
            if session is not None and member.session_id == session.session_id:
                continue
            data = player.profile or bytes(PROFILE_SIZE)
            area = self.directory.area(player.area_id)
            # The client only tests the Land word for zero ("not found").
            land_number = self.directory.lands[0].number if area is None else area.land_number
            records.append(
                pack_fixed(member.username, 16)
                + struct.pack(
                    '>4L',
                    land_number,
                    0 if area is None else area.area_id,
                    max(member.room_id, 0),
                    len(data),
                )
                + data
            )

        payload = struct.pack('>3L', 0, len(records), len(records)) + b''.join(records)
        return [
            context.reply(
                message,
                type_flags=FLAG_CHANNEL_BITS | FLAG_RESPONSE,
                command=commands.CMD_SEARCH_USERS_BY_NAME,
                payload=payload,
            )
        ]

    def _handle_search_rooms(self, context: HandlerContext, message: SnapMessage) -> list[SnapMessage]:
        """Answer the `OID` room lookup used by the Friend status.

        Records are 40 bytes (`0x001ffb10`); the client reads the rules word at
        `+28` ("Currently on a quest" or the Town number) and the room id at
        `+36` (`0x00613830`). The other words are not read.
        """

        _, conditions = _parse_search(message.payload)
        records = []
        for name, op, value in conditions:
            if name != SEARCH_OID or op != SEARCH_EQUAL:
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

    def _handle_create_game_room(self, context: HandlerContext, message: SnapMessage) -> list[SnapMessage]:
        """Create a Town or a quest room in the player's Area."""

        session = resolve_session(context, message, _LOGGER)
        if session is None:
            return []
        replay = self._replay_reliable_reply(message, session)
        if replay is not None:
            return replay

        rules = get_u32(message.payload, 0x28)
        if rules & TOWN_RULES_FLAG and self._town_category_full(context, session.lobby_id, rules):
            outbound = [
                self._result(
                    context, message, session, GameTags.START_OK, RESULT_WRAPPER_STATUS_ERROR_DIALOG, FLAG_ROOM
                )
            ]
            self._remember_reliable_reply(message, session, outbound)
            return outbound
        departures = self._detach(context, session, keep_town_listing=not rules & TOWN_RULES_FLAG)
        room = context.rooms.create_room(
            name=get_c_string(message.payload, 0),
            password=get_c_string(message.payload, 0x14),
            rules=rules,
            max_players=min(max(get_u32(message.payload, 0x10), 1), context.config.server.max_players_per_room),
            lobby_id=session.lobby_id,
            host_session_id=session.session_id,
        )
        context.sessions.set_room(session.session_id, room.room_id)
        outbound = departures + [
            self._result(context, message, session, GameTags.START_OK, room.room_id, FLAG_ROOM)
        ]
        self._remember_reliable_reply(message, session, outbound)
        return outbound

    def _handle_join(self, context: HandlerContext, message: SnapMessage) -> list[SnapMessage]:
        """Join an Area (lobby channel) or a room (room channel)."""

        session = resolve_session(context, message, _LOGGER)
        if session is None:
            return []
        channel_type = message.type_flags & FLAG_CHANNEL_BITS

        if channel_type == FLAG_CHANNEL_BITS:
            area = self.directory.area(get_u32(message.payload, 0))
            if area is None or self._area_full(session.session_id, area):
                return [
                    self._result(
                        context, message, session, GameTags.GAME_START, RESULT_WRAPPER_STATUS_ERROR_DIALOG, channel_type
                    )
                ]
            departures = self._detach(context, session, keep_town_listing=False)
            context.sessions.set_lobby(session.session_id, area.area_id)
            self._set_area(session.session_id, area.area_id)
            return departures + [
                self._result(context, message, session, GameTags.GAME_START, RESULT_WRAPPER_STATUS_OK, channel_type)
            ]

        replay = self._replay_reliable_reply(message, session)
        if replay is not None:
            return replay

        room_id = self._server_room_id(session, get_u32(message.payload, 0))
        room = context.rooms.get(room_id)
        if room is not None and session.room_id == room_id:
            return [self._result(context, message, session, GameTags.GAME_START, RESULT_WRAPPER_STATUS_OK, FLAG_ROOM)]
        if room is None:
            return [
                self._result(
                    context, message, session, GameTags.GAME_START, RESULT_WRAPPER_STATUS_ERROR_DIALOG, FLAG_ROOM
                )
            ]

        # Joining a quest room keeps the Town listing; coming back to the Town
        # that still lists the player is not a new arrival for its members.
        listed = session.session_id in room.members
        departures = self._detach(
            context,
            session,
            keep_town_listing=listed or not room.rules & TOWN_RULES_FLAG,
        )
        if not context.rooms.join(room_id, session.session_id):
            outbound = departures + [
                self._result(
                    context, message, session, GameTags.GAME_START, RESULT_WRAPPER_STATUS_ERROR_DIALOG, FLAG_ROOM
                )
            ]
            self._remember_reliable_reply(message, session, outbound)
            return outbound

        context.sessions.set_room(session.session_id, room_id)
        callbacks: list[SnapMessage] = []
        profile = self._profile(session.session_id)
        if not listed and profile is not None:
            callbacks = self._room_callbacks(
                context,
                room,
                exclude_session_id=session.session_id,
                command=commands.CMD_JOIN,
                payload=_member_record(session, profile),
            )
        outbound = departures + [
            self._result(context, message, session, GameTags.GAME_START, RESULT_WRAPPER_STATUS_OK, FLAG_ROOM)
        ] + callbacks
        self._remember_reliable_reply(message, session, outbound)
        return outbound

    def _handle_leave(self, context: HandlerContext, message: SnapMessage) -> list[SnapMessage]:
        """Leave the current room, or the Area on the lobby channel."""

        session = resolve_session(context, message, _LOGGER)
        if session is None:
            return []
        replay = self._replay_reliable_reply(message, session)
        if replay is not None:
            return replay

        channel_type = message.type_flags & FLAG_CHANNEL_BITS
        if channel_type == FLAG_CHANNEL_BITS:
            callbacks = self._detach(context, session, keep_town_listing=False)
            context.sessions.set_lobby(session.session_id, 0)
            self._set_area(session.session_id, None)
        else:
            channel_type = FLAG_ROOM
            room = context.rooms.get(session.room_id)
            departing = (
                room is not None
                and bool(room.rules & TOWN_RULES_FLAG)
                and self._is_departing(session.session_id)
            )
            callbacks = self._detach(context, session, keep_town_listing=departing)

        outbound = [
            self._result(context, message, session, GameTags.GAME_OVER, RESULT_WRAPPER_STATUS_OK, channel_type)
        ] + callbacks
        self._remember_reliable_reply(message, session, outbound)
        return outbound

    def _handle_send(self, context: HandlerContext, message: SnapMessage) -> list[SnapMessage]:
        """Relay chat and game packets to the other members of the room.

        Relays carry the sender session id in the header: the `lobby.bin`
        consumers identify the sender from callback info `+8`
        (`0x0061a03c`, `0x0061a240`).
        """

        session = resolve_session(context, message, _LOGGER)
        if session is None:
            return []

        callback_flags = message.type_flags & RELAY_CONTEXT_MASK
        if callback_flags in (TYPE_LOBBY_RELAY, TYPE_LOBBY_RELAY_REQUEST):
            ack_flags = FLAG_CHANNEL_BITS | FLAG_RESPONSE
            relay_flags = TYPE_LOBBY_RELAY | FLAG_RESPONSE
        elif callback_flags == TYPE_ROOM_RELAY:
            ack_flags = FLAG_ROOM | FLAG_RESPONSE
            relay_flags = TYPE_ROOM_RELAY | FLAG_RESPONSE
        else:
            ack_flags = FLAG_ROOM | FLAG_RESPONSE
            relay_flags = message.type_flags & ~FLAG_MULTI

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
            for member in self._room_peers(context, session)
        ]

        # Embedded sends piggyback on the outer reliable packet, which owns the
        # only transport ACK in the bundle (the child sequence word carries no
        # transport state; see the engine).
        if message.embedded_in_multi and message.type_flags & FLAG_RELIABLE:
            return relays
        return [
            context.reply(message, type_flags=ack_flags, command=commands.CMD_ACK, session_id=session.session_id)
        ] + relays

    def _handle_send_target(self, context: HandlerContext, message: SnapMessage) -> list[SnapMessage]:
        """Relay one directed packet; the receiver identifies the sender by header."""

        session = resolve_session(context, message, _LOGGER)
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
        relay_payload = build_send_target_payload(message.payload)
        target = context.sessions.get(get_u32(message.payload, 4)) if relay_payload is not None else None
        player = self._players.get(session.session_id)
        if (
            player is not None
            and target is not None
            and target.session_id == session.session_id
            and len(message.payload) == DEPARTURE_PREPARATION_SIZE
            and get_u32(message.payload, 0) == 1
            and message.payload[10] == DEPARTURE_PREPARATION_SELECTOR
        ):
            player.preparing_departure = True
        if target is None:
            _LOGGER.warning(
                'Skipping send-target relay from session 0x%08x: invalid payload or unknown target.',
                session.session_id,
            )
            return response

        response.append(
            context.direct(
                endpoint=target.endpoint,
                session_id=target.session_id,
                type_flags=FLAG_ROOM | FLAG_RELIABLE,
                command=commands.CMD_SEND_TARGET,
                payload=relay_payload,
                acknowledge_number=ack_for_session(target),
                source_session_id=session.session_id,
            )
        )
        return response

    def _handle_change_user_status(self, context: HandlerContext, message: SnapMessage) -> list[SnapMessage]:
        session = resolve_session(context, message, _LOGGER)
        if session is None:
            return []
        return [self._result(context, message, session, GameTags.RESULT2, RESULT_WRAPPER_STATUS_OK, FLAG_ROOM)]

    def _handle_change_user_property(self, context: HandlerContext, message: SnapMessage) -> list[SnapMessage]:
        """Store the member profile and publish it to everyone listing the player.

        Monster Hunter registers a property callback (slot 10, `0x0061a0b0`)
        that reads `session_id, length, profile[length]`.
        """

        session = resolve_session(context, message, _LOGGER)
        if session is None:
            return []

        player = self._players.get(session.session_id)
        if player is not None and len(message.payload) == PROFILE_SIZE:
            player.profile = bytes(message.payload)

        payload = struct.pack('>2L', session.session_id, len(message.payload)) + message.payload
        callbacks: list[SnapMessage] = []
        room = context.rooms.get(session.room_id)
        if room is not None:
            callbacks += self._room_callbacks(
                context,
                room,
                exclude_session_id=session.session_id,
                command=commands.CMD_CHANGE_USER_PROPERTY,
                payload=payload,
            )
        origin = self._origin_town(context, session)
        if origin is not None:
            callbacks += self._room_callbacks(
                context,
                origin,
                exclude_session_id=session.session_id,
                command=commands.CMD_CHANGE_USER_PROPERTY,
                payload=payload,
            )
        return [
            self._result(context, message, session, GameTags.RESULT, RESULT_WRAPPER_STATUS_OK, FLAG_ROOM)
        ] + callbacks

    def _handle_change_attribute(self, context: HandlerContext, message: SnapMessage) -> list[SnapMessage]:
        """Apply `STAT` rules changes, including the return from a quest.

        After a quest each client sends `STAT` with its origin Town rules word
        (`0x0061af10..0x0061af44`), which moves that player back into the Town
        that kept listing it.
        """

        session = resolve_session(context, message, _LOGGER)
        if session is None:
            return []

        room = context.rooms.get(session.room_id)
        if room is not None and len(message.payload) == 8 and message.payload[:4] == STAT_ATTRIBUTE:
            rules = get_u32(message.payload, 4)
            origin = self._origin_town(context, session) if _is_quest_return(room, rules) else None
            if origin is not None and origin.rules == rules:
                self._return_to_origin(context, session, room, origin)
            else:
                context.rooms.set_rules(room.room_id, rules)

        return [self._result(context, message, session, GameTags.JOIN_OK, RESULT_WRAPPER_STATUS_OK, FLAG_ROOM)]

    def _return_to_origin(
        self,
        context: HandlerContext,
        session: Session,
        quest_room: GameRoom,
        origin: GameRoom,
    ) -> None:
        """Move one returning hunter from its quest room back to its Town."""

        context.rooms.leave(quest_room.room_id, session.session_id)
        context.sessions.set_room(session.session_id, origin.room_id)
        player = self._players.get(session.session_id)
        if player is not None:
            player.room_alias = (quest_room.room_id, origin.room_id)

    def _detach(
        self,
        context: HandlerContext,
        session: Session,
        *,
        keep_town_listing: bool,
        notify: bool = True,
    ) -> list[SnapMessage]:
        """Take the player out of its current room and, if requested, its Town listing."""

        messages: list[SnapMessage] = []
        room = context.rooms.get(session.room_id) if session.room_id > 0 else None
        if session.room_id > 0:
            context.sessions.set_room(session.session_id, 0)
        if room is not None and not (keep_town_listing and room.rules & TOWN_RULES_FLAG):
            context.rooms.leave(room.room_id, session.session_id)
            if notify:
                messages += self._leave_callbacks(context, room, session.session_id)

        if not keep_town_listing:
            for town in self._listing_towns(context, session):
                context.rooms.leave(town.room_id, session.session_id)
                if notify:
                    messages += self._leave_callbacks(context, town, session.session_id)

        player = self._players.get(session.session_id)
        if player is not None:
            player.room_alias = None
            player.preparing_departure = False
        return messages

    def _leave_callbacks(self, context: HandlerContext, room: GameRoom, leaving_session_id: int) -> list[SnapMessage]:
        if _quest_started(room):
            return []
        return build_room_leave_callbacks(
            context=context,
            leaving_session_id=leaving_session_id,
            recipients=context.sessions.list_room_members(room.room_id),
        )

    def _tracking_towns(
        self, handler: Callable[[HandlerContext, SnapMessage], list[SnapMessage]]
    ) -> Callable[[HandlerContext, SnapMessage], list[SnapMessage]]:
        """Wrap a room-changing handler with the Town directory events it causes."""

        def tracked(context: HandlerContext, message: SnapMessage) -> list[SnapMessage]:
            before = self._listed_towns(context)
            return handler(context, message) + self._town_directory_updates(context, before)

        return tracked

    def _listed_towns(self, context: HandlerContext) -> dict[int, tuple[int, bytes]]:
        """Room rows the client's Town table keeps: `room_id -> (area_id, row)`."""

        return {
            room.room_id: (area.area_id, _room_row(context, room))
            for area in self.directory.areas
            for room in context.rooms.list_for_lobby(area.area_id)
            if room.rules & TOWN_RULES_FLAG and not room.rules & QUEST_STARTED_RULES_FLAG
        }

    def _town_directory_updates(
        self, context: HandlerContext, before: dict[int, tuple[int, bytes]]
    ) -> list[SnapMessage]:
        """Send added/changed Town rows (slot 4) and removed Towns (slot 5) to their Area."""

        after = self._listed_towns(context)
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
            for member in context.sessions.list_lobby_members(area_id)
            if not self._in_started_quest(context, member)
        ]

    @staticmethod
    def _in_started_quest(context: HandlerContext, session: Session) -> bool:
        room = context.rooms.get(session.room_id) if session.room_id > 0 else None
        return room is not None and _quest_started(room)

    def _room_callbacks(
        self,
        context: HandlerContext,
        room: GameRoom,
        *,
        exclude_session_id: int,
        command: int,
        payload: bytes,
    ) -> list[SnapMessage]:
        """Build one lobby callback per member physically in the room."""

        if _quest_started(room):
            return []
        return [
            context.direct(
                endpoint=member.endpoint,
                session_id=member.session_id,
                # Same unreliable callback channel as the shared join callbacks.
                type_flags=FLAG_ROOM | FLAG_RESPONSE,
                command=command,
                payload=payload,
                acknowledge_number=ack_for_session(member),
            )
            for member in context.sessions.list_room_members(room.room_id)
            if member.session_id != exclude_session_id
        ]

    def _room_peers(self, context: HandlerContext, session: Session) -> list[Session]:
        if session.room_id <= 0:
            return []
        return [
            member for member in context.sessions.list_room_members(session.room_id)
            if member.session_id != session.session_id
        ]

    def _listing_towns(self, context: HandlerContext, session: Session) -> list[GameRoom]:
        """Towns that still list the player without it being physically there."""

        return [
            room for room in context.rooms.list_for_lobby(session.lobby_id)
            if room.rules & TOWN_RULES_FLAG
            and room.room_id != session.room_id
            and session.session_id in room.members
        ]

    def _origin_town(self, context: HandlerContext, session: Session) -> GameRoom | None:
        towns = self._listing_towns(context, session)
        return towns[0] if len(towns) == 1 else None

    def _server_room_id(self, session: Session, room_id: int) -> int:
        player = self._players.get(session.session_id)
        if player is not None and player.room_alias is not None and player.room_alias[0] == room_id:
            return player.room_alias[1]
        return room_id

    def _profile(self, session_id: int) -> bytes | None:
        player = self._players.get(session_id)
        return None if player is None else player.profile

    def _is_departing(self, session_id: int) -> bool:
        """Whether the player's next Town leave is a quest departure (see above)."""

        player = self._players.get(session_id)
        if player is None:
            return False
        profile = player.profile
        return player.preparing_departure or (
            profile is not None
            and (profile[PROFILE_PARTY_STATE_OFFSET], profile[PROFILE_QUEST_STATE_OFFSET]) == PROFILE_DEPARTING_STATE
        )

    def _land_population(self, land: Land) -> int:
        area_ids = {area.area_id for area in land.areas}
        return sum(1 for player in tuple(self._players.values()) if player.area_id in area_ids)

    def _town_category_full(self, context: HandlerContext, area_id: int, rules: int) -> bool:
        """The Town directory shows at most 21 Towns per category (`0x006173a0`)."""

        category = (rules >> TOWN_CATEGORY_SHIFT) & TOWN_CATEGORY_MASK
        towns = [
            room for room in context.rooms.list_for_lobby(area_id)
            if room.rules & TOWN_RULES_FLAG and (room.rules >> TOWN_CATEGORY_SHIFT) & TOWN_CATEGORY_MASK == category
        ]
        return len(towns) >= TOWNS_PER_CATEGORY

    def _area_full(self, session_id: int, area: Area) -> bool:
        """Refuse an Area join once the Area or its Land is full, not counting the joining player.

        The client itself reports a Land as full when its summed Area
        populations reach the Land capacity (`0x00612b14`).
        """

        land = self.directory.land_of(area)
        land_area_ids = {land_area.area_id for land_area in land.areas}
        others = [player for player_id, player in tuple(self._players.items()) if player_id != session_id]
        area_population = sum(1 for player in others if player.area_id == area.area_id)
        land_population = sum(1 for player in others if player.area_id in land_area_ids)
        return area_population >= self.directory.area_capacity or land_population >= land.capacity

    def _area_population(self, area_id: int) -> int:
        return sum(1 for player in tuple(self._players.values()) if player.area_id == area_id)

    def _set_area(self, session_id: int, area_id: int | None) -> None:
        player = self._players.get(session_id)
        if player is not None:
            player.area_id = area_id

    def _forget_player(self, session_id: int) -> None:
        self._players.pop(session_id, None)
        self._app_flows.forget(session_id)
        for key in [key for key in self._reliable_replies if key[0] == session_id]:
            self._reliable_replies.pop(key, None)

    def _result(
        self,
        context: HandlerContext,
        message: SnapMessage,
        session: Session,
        selector: int,
        status: int,
        channel_type: int,
    ) -> SnapMessage:
        return context.reply(
            message,
            type_flags=channel_type | FLAG_RESPONSE,
            command=commands.CMD_RESULT_WRAPPER,
            payload=struct.pack('>2L', selector, status),
            session_id=session.session_id,
            acknowledge_number=ack_for_request(message, session),
        )

    def _replay_reliable_reply(self, message: SnapMessage, session: Session) -> list[SnapMessage] | None:
        """Replay the requester's original result for an exact reliable retry."""

        key = _reliable_reply_key(message, session)
        cached = None if key is None else self._reliable_replies.get(key)
        if cached is None or cached[0] != message.sequence_number:
            return None
        return [
            replace(cached_message, endpoint=message.endpoint)
            if cached_message.session_id == session.session_id
            else replace(cached_message)
            for cached_message in cached[1]
        ]

    def _remember_reliable_reply(self, message: SnapMessage, session: Session, outbound: list[SnapMessage]) -> None:
        key = _reliable_reply_key(message, session)
        if key is not None:
            self._reliable_replies[key] = (
                message.sequence_number,
                tuple(replace(item) for item in outbound if item.session_id == session.session_id),
            )


def _member_record(session: Session, profile: bytes) -> bytes:
    """Member record used by join callbacks and rosters (`0x00619430`)."""

    return pack_fixed(session.username, 16) + struct.pack('>2L', session.session_id, len(profile)) + profile


def _room_row(context: HandlerContext, room: GameRoom) -> bytes:
    """Room row of the directory (`kkCreateGameRoomSwap`): name16, members, 0, rules, max players, room id."""

    return struct.pack(
        '>16s5L',
        pack_fixed(room.name, 16),
        len(room.members),
        0,
        room.rules,
        min(room.max_players, context.config.server.max_players_per_room),
        room.room_id,
    )


def _quest_started(room: GameRoom) -> bool:
    return not room.rules & TOWN_RULES_FLAG and bool(room.rules & QUEST_STARTED_RULES_FLAG)


def _is_quest_return(room: GameRoom, rules: int) -> bool:
    return not room.rules & TOWN_RULES_FLAG and bool(rules & TOWN_RULES_FLAG)


def _reliable_reply_key(message: SnapMessage, session: Session) -> tuple[int, int, int] | None:
    if (message.type_flags & FLAG_RELIABLE) == 0:
        return None
    if message.embedded_in_multi:
        return None
    return (session.session_id, message.command, message.type_flags & FLAG_CHANNEL_BITS)


def _parse_search(payload: bytes) -> tuple[int, list[tuple[bytes, int, bytes]]]:
    """Parse one attribute search request into `(limit, [(name, operator, value)])`."""

    if len(payload) < SEARCH_HEADER_SIZE:
        return 0, []
    limit, count = get_u32(payload, 0), payload[4]
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


def _search_text(value: bytes) -> str:
    return value.split(b'\x00', 1)[0].decode('ascii', errors='ignore')
