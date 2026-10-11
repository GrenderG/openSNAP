"""Resident Evil Outbreak File#2 plugin tests."""

from dataclasses import replace
import os
import struct
import tempfile
import unittest
from unittest import mock

from opensnap.config import StorageConfig, default_app_config
from opensnap.core.engine import SnapProtocolEngine
from opensnap.core.sessions import Session
from opensnap.plugins.outbreak2 import Outbreak2Plugin
from opensnap.plugins.outbreak2.directory import DEFAULT_AREAS, build_directory
from opensnap.plugins.registry import identify_snap_title
from opensnap.protocol import commands
from opensnap.protocol.codec import encode_messages
from opensnap.protocol.constants import (
    FLAG_CHANNEL_BITS,
    FLAG_LOBBY,
    FLAG_RELAY,
    FLAG_RELIABLE,
    FLAG_RESPONSE,
    FLAG_ROOM,
    FOOTER_MARKER,
)
from opensnap.protocol.models import Endpoint, SnapMessage
from opensnap.storage.factory import create_storage
from tests.support import create_accounts, login_client_payload, serving

# `kkLoginClient` title codes (`netwk.bin` NA `0x0059d390`, EU `0x005bd62c`).
NA_TITLE_CODE = 0xCA19
EU_TITLE_CODE = 0xCAF3
LOBBY_FLAGS = FLAG_CHANNEL_BITS | FLAG_RELIABLE
ROOM_FLAGS = FLAG_ROOM | FLAG_RELIABLE
AREA_ID = 1
OTHER_AREA_ID = 2
STATUS_IN_SCENARIO = 1
STATUS_IN_LOBBY = 0xF7E00001
TITLED = 0x00080000
QUICK_START = 0x00100000
ROOM_RULES = TITLED | 0x00000102
STARTED = 0x40000000
BUCKET = 0x04000000
NAME_AT_LEAST = (2 << 5) | 4
NAME_AT_MOST = (2 << 5) | 6
U32_EQUAL = (0 << 5) | 1
MASK_EQUAL = (1 << 5) | 1
ACCOUNTS = tuple(f'jill{index}' for index in range(6))


def _profile(marker: int) -> bytes:
    return bytes([marker]) * 240


def _search(limit: int, *conditions: tuple[bytes, int, bytes]) -> bytes:
    """`kkSearch*` request: `u32 limit, u8 count`, padded to 8, then `key4, flags, value` per condition."""

    body = b''.join(key + bytes([flags]) + value for key, flags, value in conditions)
    return struct.pack('>LB3x', limit, len(conditions)) + body


def _oid(object_id: int) -> tuple[bytes, int, bytes]:
    return b'OID\x00', U32_EQUAL, struct.pack('>L', object_id)


def _loid(area_id: int) -> tuple[bytes, int, bytes]:
    return b'LOID', U32_EQUAL, struct.pack('>L', area_id)


def _stat(mask: int, value: int) -> tuple[bytes, int, bytes]:
    return b'STAT', MASK_EQUAL, struct.pack('>2L', mask, value)


def _room_list_query(area_id: int) -> bytes:
    """The lobby room list (`0x005a16f0`): open rooms of the titled category, limit 30."""

    return _search(30, _loid(area_id), _stat(TITLED, TITLED), _stat(TITLED, TITLED), _stat(STARTED, 0))


def _create_payload(*, name: bytes = b'raccoon', password: bytes = b'', rules: int = ROOM_RULES) -> bytes:
    payload = name.ljust(16, b'\x00') + struct.pack('>L', 4) + password.ljust(16, b'\x00') + struct.pack('>2L', 1, rules)
    assert len(payload) == 44
    return payload


def _join_payload(room_id: int, password: bytes = b'') -> bytes:
    return struct.pack('>2L', room_id, 1) + password.ljust(16, b'\x00')


def _search_records(payload: bytes) -> list[tuple]:
    count = struct.unpack_from('>L', payload, 4)[0]
    return [struct.unpack_from('>16s6L', payload, 12 + 40 * index) for index in range(count)]


class Outbreak2LobbyTests(unittest.TestCase):
    """Areas, rooms and scenarios through the protocol engine."""

    def setUp(self) -> None:
        temp_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temp_directory.cleanup)
        self.config = replace(
            serving(default_app_config(), 'outbreak2'),
            storage=StorageConfig(backend='sqlite', sqlite_path=f'{temp_directory.name}/ob2.sqlite'),
        )
        create_accounts(self.config, *ACCOUNTS)
        self.engine: SnapProtocolEngine | None = None
        self._start()
        self.players = [self._login(f'jill{index}', f'10.0.0.{index + 1}', index + 1) for index in range(3)]

    def _start(self, **settings: str) -> None:
        """(Re)create the engine with only the given `OPENSNAP_OUTBREAK2_*` settings."""

        if self.engine is not None:
            self.engine.close()
        with mock.patch.dict(os.environ, settings):
            for key in [key for key in os.environ if key.startswith('OPENSNAP_OUTBREAK2_') and key not in settings]:
                del os.environ[key]
            self.plugin = Outbreak2Plugin()
            self.engine = SnapProtocolEngine(config=self.config, plugin=self.plugin, role='combined')
        self.addCleanup(self.engine.close)
        self.sequences: dict[int, int] = {}
        self.unreliable_sequences: dict[int, int] = {}

    def _login(
        self, username: str, host: str, marker: int, *, title_code: int = NA_TITLE_CODE, area_id: int | None = AREA_ID
    ) -> tuple[Endpoint, int]:
        """Bootstrap login, KICS login (no separate status call), then the Area join."""

        endpoint = Endpoint(host=host, port=2000)
        login = self._send_raw(
            endpoint, 0, commands.CMD_LOGIN_CLIENT, login_client_payload(username.encode(), title_code), FLAG_CHANNEL_BITS
        )
        session_id = login[0].session_id
        kics = bytes(0x128) + _profile(marker) + bytes(14)
        result = self._send((endpoint, session_id), commands.CMD_LOGIN_TO_KICS, kics, FLAG_CHANNEL_BITS)
        self.assertEqual(result[0].command, commands.CMD_RESULT_LOGIN_TO_KICS)
        player = (endpoint, session_id)
        if area_id is not None:
            joined = self._join_area(player, area_id)
            self.assertEqual(struct.unpack('>2L', self._to(joined, player, commands.CMD_RESULT_WRAPPER)[0].payload),
                             (commands.CMD_JOIN, 0))
        return player

    def _login_eu(self, username: str, host: str, marker: int, **kwargs) -> tuple[Endpoint, int]:
        return self._login(username, host, marker, title_code=EU_TITLE_CODE, **kwargs)

    def _join_area(self, player, area_id: int) -> list[SnapMessage]:
        return self._send(player, commands.CMD_JOIN, struct.pack('>L', area_id), LOBBY_FLAGS)

    def _send_raw(self, endpoint, session_id, command, payload, type_flags, sequence=None) -> list[SnapMessage]:
        if sequence is None:
            # The client numbers reliable and unreliable packets separately.
            counters = self.sequences if type_flags & FLAG_RELIABLE else self.unreliable_sequences
            sequence = counters.get(session_id, 0) + 1
            counters[session_id] = sequence
        message = SnapMessage(
            endpoint=endpoint,
            type_flags=type_flags,
            packet_number=3,
            command=command,
            session_id=session_id,
            sequence_number=sequence,
            acknowledge_number=0,
            payload=payload,
        )
        result = self.engine.handle_datagram(encode_messages([message]), endpoint)
        self.assertFalse(result.errors)
        if command == commands.CMD_LOGIN_TO_KICS:
            # The client restarts its reliable numbering at 0 when it sends the KICS login.
            self.sequences[session_id] = -1
        return result.messages

    def _send(self, player, command, payload=b'', type_flags=ROOM_FLAGS, sequence=None):
        endpoint, session_id = player
        return self._send_raw(endpoint, session_id, command, payload, type_flags, sequence)

    def _create_room(self, player, **kwargs) -> int:
        result = self._send(player, commands.CMD_CREATE_GAME_ROOM, _create_payload(**kwargs), LOBBY_FLAGS)
        selector, room_id = struct.unpack('>2L', self._to(result, player, commands.CMD_RESULT_WRAPPER)[0].payload)
        self.assertEqual(selector, commands.CMD_CREATE_GAME_ROOM)
        return room_id

    def _join_room(self, player, room_id: int, password: bytes = b'') -> list[SnapMessage]:
        return self._send(player, commands.CMD_JOIN, _join_payload(room_id, password))

    def _set_status(self, player, status: int) -> list[SnapMessage]:
        return self._send(player, commands.CMD_CHANGE_USER_STATUS, struct.pack('>L', status))

    def _start_scenario(self, host, *members) -> None:
        self._send(host, commands.CMD_CHANGE_ATTRIBUTE, b'STAT' + struct.pack('>L', STARTED | BUCKET))
        for player in (host, *members):
            result = self._set_status(player, STATUS_IN_SCENARIO)
            self.assertEqual(struct.unpack('>2L', result[0].payload), (commands.CMD_CHANGE_USER_STATUS, 0))

    def _roster(self, player, room_id: int) -> dict[int, bytes]:
        roster = self._send(player, commands.CMD_QUERY_USER, struct.pack('>L', room_id))[0].payload
        count = struct.unpack_from('>L', roster, 4)[0]
        records = [roster[12 + 264 * index:12 + 264 * (index + 1)] for index in range(count)]
        return {struct.unpack_from('>L', record, 16)[0]: record for record in records}

    def _search_rooms(self, player, payload: bytes) -> SnapMessage:
        return self._send(player, commands.CMD_SEARCH_ROOMS, payload, LOBBY_FLAGS)[0]

    @staticmethod
    def _to(messages: list[SnapMessage], player, command: int) -> list[SnapMessage]:
        return [message for message in messages if message.session_id == player[1] and message.command == command]

    @staticmethod
    def _sent_to(messages: list[SnapMessage], player) -> list[SnapMessage]:
        return [message for message in messages if message.session_id == player[1]]

    # Areas.

    def test_area_query_lists_ten_areas_and_counts_users(self) -> None:
        self._login_eu('jill3', '10.0.0.4', 4, area_id=OTHER_AREA_ID)
        player = self.players[0]
        query = _search(
            10, (b'NAME', NAME_AT_LEAST, b'AREA01'.ljust(16, b'\x00')), (b'NAME', NAME_AT_MOST, b'AREA10'.ljust(16, b'\x00'))
        )
        reply = self._send(player, commands.CMD_QUERY_AREA, query, LOBBY_FLAGS)[0]
        self.assertEqual(reply.type_flags, FLAG_CHANNEL_BITS | FLAG_RESPONSE | FLAG_RELIABLE)
        self.assertEqual(struct.unpack_from('>3L', reply.payload), (10, 1, 10))
        self.assertEqual(len(reply.payload), 12 + 10 * 36)
        rows = [struct.unpack_from('>16s5L', reply.payload, 12 + 36 * index) for index in range(10)]
        self.assertEqual([row[0].rstrip(b'\x00') for row in rows], [f'AREA{n:02d}'.encode() for n in range(1, 11)])
        self.assertEqual([row[5] for row in rows], list(range(1, 11)))
        self.assertEqual([row[1] for row in rows], [3, 1] + [0] * 8)
        self.assertTrue(all(row[2:5] == (400, 400, 0) for row in rows))

        lookup = self._send(player, commands.CMD_QUERY_AREA, _search(1, _oid(OTHER_AREA_ID)), LOBBY_FLAGS)[0]
        self.assertEqual(struct.unpack_from('>3L', lookup.payload), (1, 1, 1))
        self.assertEqual(struct.unpack_from('>16s5L', lookup.payload, 12),
                         (b'AREA02'.ljust(16, b'\x00'), 1, 400, 400, 0, OTHER_AREA_ID))

    def test_attribute_replies_echo_the_asked_name(self) -> None:
        host = self.players[0]
        area = self._send(host, commands.CMD_QUERY_ATTRIBUTE, struct.pack('>L', AREA_ID) + b'USER', LOBBY_FLAGS)[0]
        self.assertEqual(area.type_flags, FLAG_CHANNEL_BITS | FLAG_RESPONSE | FLAG_RELIABLE)
        self.assertEqual(area.payload, struct.pack('>L4sL', AREA_ID, b'USER', 3))

        room_id = self._create_room(host)
        user = self._send(host, commands.CMD_QUERY_ATTRIBUTE, struct.pack('>L', room_id) + b'USER')[0]
        maxi = self._send(host, commands.CMD_QUERY_ATTRIBUTE, struct.pack('>L', room_id) + b'MAXI')[0]
        self.assertEqual(user.type_flags, FLAG_ROOM | FLAG_RESPONSE | FLAG_RELIABLE)
        self.assertEqual(user.payload, struct.pack('>L4sL', room_id, b'USER', 1))
        self.assertEqual(maxi.payload, struct.pack('>L4sL', room_id, b'MAXI', 4))

    def test_full_server_refuses_new_area_joins_but_allows_moves(self) -> None:
        self._start(OPENSNAP_OUTBREAK2_CAPACITY='2')
        first = self._login('jill0', '10.0.0.1', 1)
        self._login_eu('jill1', '10.0.0.2', 2, area_id=OTHER_AREA_ID)
        third = self._login('jill2', '10.0.0.3', 3, area_id=None)

        refused = self._join_area(third, AREA_ID)
        error = self._to(refused, third, commands.CMD_RESULT_ERROR)
        self.assertEqual(len(error), 1)
        self.assertEqual(struct.unpack('>2L', error[0].payload), (commands.CMD_JOIN, 1))
        self.assertEqual(error[0].type_flags, FLAG_CHANNEL_BITS | FLAG_RESPONSE | FLAG_RELIABLE)
        self.assertEqual(self.engine._sessions.get(third[1]).lobby_id, 0)  # noqa: SLF001

        moved = self._join_area(first, 3)
        self.assertEqual(struct.unpack('>2L', self._to(moved, first, commands.CMD_RESULT_WRAPPER)[0].payload),
                         (commands.CMD_JOIN, 0))
        users = self._send(first, commands.CMD_QUERY_ATTRIBUTE, struct.pack('>L', 3) + b'USER', LOBBY_FLAGS)[0]
        self.assertEqual(users.payload, struct.pack('>L4sL', 3, b'USER', 1))
        left = self._send(first, commands.CMD_QUERY_ATTRIBUTE, struct.pack('>L', AREA_ID) + b'USER', LOBBY_FLAGS)[0]
        self.assertEqual(left.payload, struct.pack('>L4sL', AREA_ID, b'USER', 0))

    # Rooms.

    def test_full_room_roster_is_paged_to_fit_the_client_receive_buffer(self) -> None:
        # A 4-player roster (1068 bytes) exceeds the client's 896-byte receive
        # buffer: 3 records with packet number 1 and the room flag clear, then 1
        # record with the room flag; the roster callback counts records per page.
        host, guest, third = self.players
        fourth = self._login('jill3', '10.0.0.4', 4)
        room_id = self._create_room(host)
        for player in (guest, third, fourth):
            self._join_room(player, room_id)
        members = [host, guest, third, fourth]

        pages = self._send(host, commands.CMD_QUERY_USER, struct.pack('>L', room_id))

        # The last page keeps the room flag, so its packet number is not read.
        self.assertEqual([page.type_flags for page in pages], [FLAG_RESPONSE | FLAG_RELIABLE, FLAG_ROOM | FLAG_RESPONSE | FLAG_RELIABLE])
        self.assertNotEqual(pages[0].packet_number, 0)
        sids = []
        for page in pages:
            self.assertLessEqual(len(encode_messages([page])), 896)
            room, total, count = struct.unpack_from('>3L', page.payload)
            self.assertEqual((room, total, len(page.payload)), (room_id, 4, 12 + count * 264))
            sids += [struct.unpack_from('>L', page.payload, 12 + index * 264 + 16)[0] for index in range(count)]
        self.assertEqual([struct.unpack_from('>L', page.payload, 8)[0] for page in pages], [3, 1])
        self.assertEqual(sorted(sids), sorted(player[1] for player in members))

    def test_room_create_join_and_roster(self) -> None:
        host, guest, watcher = self.players
        created = self._send(host, commands.CMD_CREATE_GAME_ROOM, _create_payload(), LOBBY_FLAGS)
        wrapper = self._to(created, host, commands.CMD_RESULT_WRAPPER)[0]
        selector, room_id = struct.unpack('>2L', wrapper.payload)
        self.assertEqual(selector, commands.CMD_CREATE_GAME_ROOM)
        # Area peers get the new row (slot 4, 36 bytes).
        row = self._to(created, watcher, commands.CMD_CREATE_GAME_ROOM)[0].payload
        self.assertEqual(struct.unpack('>16s5L', row),
                         (b'raccoon'.ljust(16, b'\x00'), 1, 0, ROOM_RULES, 4, room_id))

        joined = self._join_room(guest, room_id)
        result = self._to(joined, guest, commands.CMD_RESULT_WRAPPER)[0]
        self.assertEqual(struct.unpack('>2L', result.payload), (commands.CMD_JOIN, 0))
        self.assertEqual(result.type_flags, FLAG_ROOM | FLAG_RESPONSE | FLAG_RELIABLE)
        arrival = self._to(joined, host, commands.CMD_JOIN)[0]
        self.assertEqual(arrival.type_flags, FLAG_ROOM | FLAG_RESPONSE)
        self.assertEqual(arrival.payload, b'jill1'.ljust(16, b'\x00') + struct.pack('>2L', guest[1], 240) + _profile(2))
        self.assertFalse(self._to(joined, guest, commands.CMD_JOIN))

        roster = self._roster(host, room_id)
        self.assertEqual(sorted(roster), sorted([host[1], guest[1]]))
        self.assertEqual(roster[host[1]], b'jill0'.ljust(16, b'\x00') + struct.pack('>2L', host[1], 240) + _profile(1))
        self.assertEqual(roster[guest[1]][24:], _profile(2))

    def test_roster_for_room_id_zero_resolves_to_the_callers_room_after_a_scenario(self) -> None:
        # Back from a scenario the client queries its roster with room id 0
        # (`0x005a2150`); without resolving it the meeting room shows "0P".
        host, guest, watcher = self.players
        room_id = self._create_room(host)
        self._join_room(guest, room_id)
        self._start_scenario(host, guest)
        for player in (host, guest):
            self._set_status(player, STATUS_IN_LOBBY)

        resolved = self._roster(host, 0)
        self.assertEqual(sorted(resolved), sorted([host[1], guest[1]]))
        # A player in the Area but no room still gets an empty roster for id 0.
        self.assertEqual(self._roster(watcher, 0), {})

    def test_room_search_fits_one_reply_of_the_client_receive_buffer(self) -> None:
        # `kkSearchRooms` replies go through a one-shot dispatcher keyed on the
        # packet number (`0x005a3aa0`), so the list cannot be paged; the load
        # counters ask for up to 250 rows.
        for index in range(30):
            self.engine._rooms.create_room(  # noqa: SLF001
                name=f'room{index}', password='', rules=ROOM_RULES, max_players=4, lobby_id=AREA_ID,
                host_session_id=0x1000 + index,
            )

        for limit in (30, 250):
            listing = self._search_rooms(self.players[0], _search(limit, _loid(AREA_ID)))
            self.assertEqual(struct.unpack_from('>L', listing.payload, 8)[0], 21)
            self.assertLessEqual(len(encode_messages([listing])), 896)

    def test_room_search_lists_open_titled_rooms_and_counts_started_ones(self) -> None:
        host, guest, watcher = self.players
        room_id = self._create_room(host)
        self._join_room(guest, room_id)

        listing = self._search_rooms(watcher, _room_list_query(AREA_ID))
        self.assertEqual(listing.type_flags, FLAG_CHANNEL_BITS | FLAG_RESPONSE | FLAG_RELIABLE)
        self.assertEqual(struct.unpack_from('>3L', listing.payload), (0, 1, 1))
        self.assertEqual(len(listing.payload), 12 + 40)
        record = (b'raccoon'.ljust(16, b'\x00'), 0, 2, 0, ROOM_RULES, 4, room_id)
        self.assertEqual(_search_records(listing.payload), [record])

        # A quick-start room (no titled bit) is only found by its own category.
        quick_id = self._create_room(watcher, name=b'quick', rules=QUICK_START)
        listing = self._search_rooms(guest, _room_list_query(AREA_ID))
        self.assertEqual([entry[6] for entry in _search_records(listing.payload)], [room_id])
        quick = self._search_rooms(guest, _search(30, _loid(AREA_ID), _stat(QUICK_START, QUICK_START), _stat(STARTED, 0)))
        self.assertEqual([entry[6] for entry in _search_records(quick.payload)], [quick_id])
        # Another Area has no rooms.
        other = self._search_rooms(guest, _room_list_query(OTHER_AREA_ID))
        self.assertEqual(struct.unpack_from('>3L', other.payload), (0, 0, 0))

        # Starting the room: it leaves the list and the Area gets a delete.
        started = self._send(host, commands.CMD_CHANGE_ATTRIBUTE, b'STAT' + struct.pack('>L', STARTED | BUCKET))
        self.assertEqual(struct.unpack('>2L', self._to(started, host, commands.CMD_RESULT_WRAPPER)[0].payload),
                         (commands.CMD_CHANGE_ATTRIBUTE, 0))
        delete = self._to(started, watcher, commands.CMD_DELETE)
        self.assertEqual([message.payload for message in delete], [struct.pack('>L', room_id)])
        listing = self._search_rooms(watcher, _room_list_query(AREA_ID))
        self.assertEqual(struct.unpack_from('>3L', listing.payload), (0, 0, 0))

        late = self._login('jill3', '10.0.0.4', 4)
        refused = self._join_room(late, room_id)
        self.assertEqual(struct.unpack('>2L', self._to(refused, late, commands.CMD_RESULT_ERROR)[0].payload),
                         (commands.CMD_JOIN, 1))

        counters = self._search_rooms(
            watcher, _search(30, _loid(AREA_ID), _stat(STARTED, STARTED), _stat(BUCKET, BUCKET))
        )
        started_record = (b'raccoon'.ljust(16, b'\x00'), 0, 2, 0, STARTED | BUCKET, 4, room_id)
        self.assertEqual(_search_records(counters.payload), [started_record])
        by_id = self._search_rooms(watcher, _search(1, _oid(room_id)))
        self.assertEqual(_search_records(by_id.payload), [started_record])

    def test_wrong_password_is_error_code_15(self) -> None:
        host, guest, _ = self.players
        room_id = self._create_room(host, password=b'leon')
        refused = self._join_room(guest, room_id, b'claire')
        error = self._to(refused, guest, commands.CMD_RESULT_ERROR)
        self.assertEqual(struct.unpack('>2L', error[0].payload), (commands.CMD_JOIN, 15))
        self.assertEqual(error[0].type_flags, FLAG_ROOM | FLAG_RESPONSE | FLAG_RELIABLE)
        accepted = self._join_room(guest, room_id, b'leon')
        self.assertEqual(struct.unpack('>2L', self._to(accepted, guest, commands.CMD_RESULT_WRAPPER)[0].payload),
                         (commands.CMD_JOIN, 0))
        self.assertIn(guest[1], self._roster(host, room_id))

    def test_reliable_create_retry_replays_the_same_room(self) -> None:
        host = self.players[0]
        sequence = self.sequences[host[1]] + 1
        self.sequences[host[1]] = sequence
        first = self._send(host, commands.CMD_CREATE_GAME_ROOM, _create_payload(), LOBBY_FLAGS, sequence=sequence)
        retry = self._send(host, commands.CMD_CREATE_GAME_ROOM, _create_payload(), LOBBY_FLAGS, sequence=sequence)
        wrappers = self._to(first + retry, host, commands.CMD_RESULT_WRAPPER)
        self.assertEqual(len(wrappers), 2)
        self.assertEqual(wrappers[0].payload, wrappers[1].payload)
        self.assertEqual(wrappers[0].sequence_number, wrappers[1].sequence_number)
        everything = self._search_rooms(host, _search(30, _loid(AREA_ID)))
        self.assertEqual(struct.unpack_from('>L', everything.payload, 4)[0], 1)

    # Scenarios.

    def test_scenario_window_relays_game_packets_and_keeps_the_room(self) -> None:
        host, guest, watcher = self.players
        room_id = self._create_room(host)
        self._join_room(guest, room_id)
        self._start_scenario(host, guest)

        # Game packets are relayed unchanged with the sender id, never echoed.
        relay = self._send(guest, commands.CMD_SEND, b'\x02\x10\x00\x07', FLAG_ROOM | FLAG_RELIABLE)
        to_host = self._to(relay, host, commands.CMD_SEND)
        self.assertEqual(len(to_host), 1)
        self.assertEqual((to_host[0].type_flags, to_host[0].source_session_id, to_host[0].payload),
                         (ROOM_FLAGS, guest[1], b'\x02\x10\x00\x07'))
        self.assertFalse(self._to(relay, guest, commands.CMD_SEND))

        # Lobby callbacks reach nobody in the scenario.
        quiet = self._send(watcher, commands.CMD_CREATE_GAME_ROOM, _create_payload(name=b'other'), LOBBY_FLAGS)
        quiet += self._send(guest, commands.CMD_CHANGE_USER_PROPERTY, _profile(5))
        self.assertFalse(self._sent_to(quiet, host))
        self.assertEqual([message.command for message in self._sent_to(quiet, guest)], [commands.CMD_RESULT_WRAPPER])

        # Back in the lobby the host gets lobby callbacks again and is still in its room.
        self._set_status(host, STATUS_IN_LOBBY)
        created = self._send(watcher, commands.CMD_CREATE_GAME_ROOM, _create_payload(name=b'third'), LOBBY_FLAGS)
        self.assertTrue(self._to(created, host, commands.CMD_CREATE_GAME_ROOM))
        self.assertFalse(self._sent_to(created, guest))
        self.assertEqual(self.engine._sessions.get(host[1]).room_id, room_id)  # noqa: SLF001
        self.assertEqual(sorted(self._roster(host, room_id)), sorted([host[1], guest[1]]))

    # Chat and relays.

    def test_room_and_area_chat_reach_their_peers_only(self) -> None:
        host, guest, watcher = self.players
        room_id = self._create_room(host)
        self._join_room(guest, room_id)
        playing = self._login('jill3', '10.0.0.4', 4)
        self._create_room(playing, name=b'busy')
        self._start_scenario(playing)
        elsewhere = self._login_eu('jill4', '10.0.0.5', 5, area_id=OTHER_AREA_ID)

        room_chat = self._send(host, commands.CMD_SEND, b'hello', FLAG_ROOM | FLAG_RELAY | FLAG_RELIABLE)
        relays = [message for message in room_chat if message.command == commands.CMD_SEND]
        self.assertEqual([message.session_id for message in relays], [guest[1]])
        self.assertEqual((relays[0].source_session_id, relays[0].payload), (host[1], b'hello'))

        area_chat = self._send(watcher, commands.CMD_SEND, b'anyone?', FLAG_CHANNEL_BITS | FLAG_RELAY | FLAG_RELIABLE)
        relays = [message for message in area_chat if message.command == commands.CMD_SEND]
        # Players inside rooms hear the Area channel; the sender, the scenario and other Areas do not.
        self.assertEqual(sorted(message.session_id for message in relays), sorted([host[1], guest[1]]))
        self.assertTrue(all(message.type_flags == FLAG_LOBBY | FLAG_RELAY | FLAG_RESPONSE for message in relays))
        self.assertTrue(all(message.source_session_id == watcher[1] for message in relays))
        self.assertNotIn(elsewhere[1], [message.session_id for message in area_chat])

        from_room = self._send(guest, commands.CMD_SEND, b'hi', FLAG_CHANNEL_BITS | FLAG_RELAY | FLAG_RELIABLE)
        self.assertEqual(sorted(message.session_id for message in from_room if message.command == commands.CMD_SEND),
                         sorted([host[1], watcher[1]]))

    def test_send_target_relays_to_the_target_with_its_word_zeroed(self) -> None:
        host, guest, watcher = self.players
        room_id = self._create_room(host)
        self._join_room(guest, room_id)

        for type_flags, target in ((FLAG_RELIABLE | FLAG_ROOM, guest), (FLAG_RELIABLE | FLAG_CHANNEL_BITS | FLAG_RELAY, watcher)):
            with self.subTest(type_flags=hex(type_flags)):
                payload = struct.pack('>2L', 1, target[1]) + b'\x00\x00\x01\x00'
                result = self._send(host, commands.CMD_SEND_TARGET, payload, type_flags)
                relays = [message for message in result if message.command == commands.CMD_SEND_TARGET]
                self.assertEqual(len(relays), 1)
                self.assertEqual(relays[0].session_id, target[1])
                self.assertEqual(relays[0].type_flags, type_flags)
                self.assertEqual(relays[0].source_session_id, host[1])
                self.assertEqual(relays[0].payload, payload[:4] + bytes(4) + payload[8:])

    def test_friend_search_reports_area_and_room(self) -> None:
        host, guest, _ = self.players
        room_id = self._create_room(host)
        found = self._send(guest, commands.CMD_SEARCH_USERS_BY_NAME, b'jill0-'.ljust(24, b'\x00'), LOBBY_FLAGS)[0]
        self.assertEqual(found.type_flags, FLAG_CHANNEL_BITS | FLAG_RESPONSE | FLAG_RELIABLE)
        self.assertEqual(struct.unpack_from('>3L', found.payload), (0, 1, 1))
        self.assertEqual(found.payload[12:],
                         b'jill0'.ljust(16, b'\x00') + struct.pack('>4L', 1, AREA_ID, room_id, 240) + _profile(1))

        missing = self._send(guest, commands.CMD_SEARCH_USERS_BY_NAME, b'wesker-'.ljust(24, b'\x00'), LOBBY_FLAGS)[0]
        self.assertEqual(missing.payload, struct.pack('>3L', 0, 0, 0))

    # Regions.

    def test_na_and_eu_players_share_areas_and_rooms(self) -> None:
        self.assertEqual(identify_snap_title(NA_TITLE_CODE, FOOTER_MARKER)[0], 'outbreak2')
        self.assertEqual(identify_snap_title(EU_TITLE_CODE, FOOTER_MARKER)[0], 'outbreak2')

        na_host, _, na_watcher = self.players
        eu_guest = self._login_eu('jill3', '10.0.0.4', 4)
        room_id = self._create_room(na_host)
        joined = self._join_room(eu_guest, room_id)
        self.assertEqual(struct.unpack('>2L', self._to(joined, eu_guest, commands.CMD_RESULT_WRAPPER)[0].payload),
                         (commands.CMD_JOIN, 0))
        self.assertEqual(sorted(self._roster(eu_guest, room_id)), sorted([na_host[1], eu_guest[1]]))

        for viewer in (eu_guest, na_watcher):
            listing = self._search_rooms(viewer, _room_list_query(AREA_ID))
            self.assertEqual([(entry[2], entry[6]) for entry in _search_records(listing.payload)], [(2, room_id)])

        self._start_scenario(na_host, eu_guest)
        for sender, receiver in ((na_host, eu_guest), (eu_guest, na_host)):
            relay = self._send(sender, commands.CMD_SEND, b'\x01\x02', FLAG_ROOM | FLAG_RELIABLE)
            delivered = self._to(relay, receiver, commands.CMD_SEND)
            self.assertEqual([(message.source_session_id, message.payload) for message in delivered],
                             [(sender[1], b'\x01\x02')])

    # Departures.

    def test_logout_and_timeout_tell_the_remaining_members(self) -> None:
        host, guest, watcher = self.players
        room_id = self._create_room(host)
        self._join_room(guest, room_id)
        self._join_room(watcher, room_id)

        logout = self._send(guest, commands.CMD_LOGOUT_CLIENT, b'', FLAG_CHANNEL_BITS)
        # Room leave callbacks (slot 6); the Area leave (slot 7) travels on the lobby channel.
        leaves = [message for message in logout
                  if message.command == commands.CMD_LEAVE and message.type_flags & FLAG_CHANNEL_BITS == FLAG_ROOM]
        self.assertEqual(sorted(message.session_id for message in leaves), sorted([host[1], watcher[1]]))
        self.assertTrue(all(message.payload == struct.pack('>L', guest[1]) for message in leaves))
        self.assertEqual(sorted(self._roster(host, room_id)), sorted([host[1], watcher[1]]))

        with self.assertLogs('opensnap.engine', 'WARNING'):
            timeout = self.engine.handle_transport_timeout(*watcher)
        leaves = [message for message in timeout
                  if message.command == commands.CMD_LEAVE and message.type_flags & FLAG_CHANNEL_BITS == FLAG_ROOM]
        self.assertEqual([(message.session_id, message.payload) for message in leaves],
                         [(host[1], struct.pack('>L', watcher[1]))])
        self.assertEqual(sorted(self._roster(host, room_id)), [host[1]])

    def test_online_players_follow_login_area_and_logout(self) -> None:
        storage = create_storage(self.config)
        self.addCleanup(storage.close)
        online = storage.online_players
        player = self._login('jill3', '10.0.0.4', 4, area_id=None)
        rows = {row.session_id: row for row in online.list('outbreak2')}
        self.assertEqual((rows[player[1]].username, rows[player[1]].area_id), ('jill3', 0))
        self.assertEqual(rows[self.players[0][1]].area_id, AREA_ID)

        self._join_area(player, OTHER_AREA_ID)
        rows = {row.session_id: row for row in online.list('outbreak2')}
        self.assertEqual(rows[player[1]].area_id, OTHER_AREA_ID)

        self._send(player, commands.CMD_LOGOUT_CLIENT, b'', FLAG_CHANNEL_BITS)
        self.assertNotIn(player[1], [row.session_id for row in online.list('outbreak2')])

    # Area callbacks.

    AREA_CALLBACK_FLAGS = FLAG_CHANNEL_BITS | FLAG_RESPONSE

    def _area_callbacks(self, messages: list[SnapMessage], command: int) -> list[tuple[int, bytes]]:
        """`(recipient, payload)` of the Area join/leave callbacks (lobby channel) in a reply list."""

        return sorted(
            (message.session_id, message.payload) for message in messages
            if message.command == command and message.type_flags == self.AREA_CALLBACK_FLAGS
        )

    def test_area_join_tells_the_area_members_outside_a_scenario(self) -> None:
        first, second, playing = self.players
        self._create_room(playing, name=b'busy')
        self._start_scenario(playing)
        elsewhere = self._login_eu('jill3', '10.0.0.4', 4, area_id=OTHER_AREA_ID)
        joiner = self._login('jill4', '10.0.0.5', 5, area_id=None)

        joined = self._join_area(joiner, AREA_ID)
        payload = struct.pack('>L', joiner[1])
        self.assertEqual(self._area_callbacks(joined, commands.CMD_JOIN),
                         sorted([(first[1], payload), (second[1], payload)]))
        self.assertFalse(self._sent_to(joined, elsewhere))
        self.assertFalse(self._sent_to(joined, playing))
        self.assertEqual([message.command for message in self._sent_to(joined, joiner)], [commands.CMD_RESULT_WRAPPER])

    def test_area_leave_logout_and_timeout_tell_the_remaining_members(self) -> None:
        first, second, third = self.players
        elsewhere = self._login_eu('jill3', '10.0.0.4', 4, area_id=OTHER_AREA_ID)
        late = self._login('jill4', '10.0.0.5', 5)

        left = self._send(late, commands.CMD_LEAVE, b'', LOBBY_FLAGS)
        payload = struct.pack('>L', late[1])
        self.assertEqual(self._area_callbacks(left, commands.CMD_LEAVE),
                         sorted([(first[1], payload), (second[1], payload), (third[1], payload)]))
        self.assertFalse(self._sent_to(left, elsewhere))
        self.assertEqual(self.engine._sessions.get(late[1]).lobby_id, 0)  # noqa: SLF001

        logout = self._send(third, commands.CMD_LOGOUT_CLIENT, b'', FLAG_CHANNEL_BITS)
        payload = struct.pack('>L', third[1])
        self.assertEqual(self._area_callbacks(logout, commands.CMD_LEAVE),
                         sorted([(first[1], payload), (second[1], payload)]))
        self.assertFalse(self._sent_to(logout, elsewhere))

        with self.assertLogs('opensnap.engine', 'WARNING'):
            timeout = self.engine.handle_transport_timeout(*second)
        self.assertEqual(self._area_callbacks(timeout, commands.CMD_LEAVE), [(first[1], struct.pack('>L', second[1]))])
        self.assertFalse(self._sent_to(timeout, elsewhere))

    def test_moving_between_areas_leaves_one_and_joins_the_other(self) -> None:
        mover, stayer, _ = self.players
        elsewhere = self._login_eu('jill3', '10.0.0.4', 4, area_id=OTHER_AREA_ID)
        moved = self._join_area(mover, OTHER_AREA_ID)
        payload = struct.pack('>L', mover[1])
        leaves = self._area_callbacks(moved, commands.CMD_LEAVE)
        self.assertEqual(leaves, sorted([(stayer[1], payload), (self.players[2][1], payload)]))
        self.assertEqual(self._area_callbacks(moved, commands.CMD_JOIN), [(elsewhere[1], payload)])
        self.assertFalse(self._to(moved, mover, commands.CMD_JOIN))
        self.assertFalse(self._to(moved, mover, commands.CMD_LEAVE))

    # Deferred room leave callbacks.

    def test_room_leaves_during_a_scenario_wait_for_the_lobby_status(self) -> None:
        host, guest, watcher = self.players
        fourth = self._login('jill3', '10.0.0.4', 4)
        room_id = self._create_room(host)
        for member in (guest, watcher, fourth):
            self._join_room(member, room_id)
        self._start_scenario(host, guest)

        logout = self._send(watcher, commands.CMD_LOGOUT_CLIENT, b'', FLAG_CHANNEL_BITS)
        with self.assertLogs('opensnap.engine', 'WARNING'):
            timeout = self.engine.handle_transport_timeout(*fourth)
        for player in (host, guest):
            self.assertFalse(self._sent_to(logout + timeout, player))

        def room_leaves(messages: list[SnapMessage], player) -> list[bytes]:
            return sorted(message.payload for message in self._to(messages, player, commands.CMD_LEAVE)
                          if message.type_flags == FLAG_ROOM | FLAG_RESPONSE)

        departed = sorted([struct.pack('>L', watcher[1]), struct.pack('>L', fourth[1])])
        back = self._set_status(host, STATUS_IN_LOBBY)
        self.assertEqual(struct.unpack('>2L', self._to(back, host, commands.CMD_RESULT_WRAPPER)[0].payload),
                         (commands.CMD_CHANGE_USER_STATUS, 0))
        self.assertEqual(room_leaves(back, host), departed)
        self.assertFalse(self._sent_to(back, guest))
        # Delivered once.
        self.assertEqual(room_leaves(self._set_status(host, STATUS_IN_LOBBY), host), [])

        guest_back = self._set_status(guest, STATUS_IN_LOBBY)
        self.assertEqual(room_leaves(guest_back, guest), departed)
        self.assertFalse(self._sent_to(guest_back, host))
        self.assertEqual(sorted(self._roster(host, room_id)), sorted([host[1], guest[1]]))

    # Idle limits.

    def test_idle_limits(self) -> None:
        host, guest, _ = self.players
        context = self.engine._context  # noqa: SLF001
        lobby = self.engine._sessions.get(host[1])  # noqa: SLF001
        self.assertEqual(self.plugin.session_idle_limit(context, lobby), 300)
        self._create_room(guest)
        self._start_scenario(guest)
        playing = self.engine._sessions.get(guest[1])  # noqa: SLF001
        self.assertEqual(self.plugin.session_idle_limit(context, playing), 300)
        unknown = Session(session_id=0x0BADBEEF, user_id=99, username='nobody', endpoint=Endpoint('10.0.0.99', 2000))
        self.assertIsNone(self.plugin.session_idle_limit(context, unknown))

    # Credential check login.

    def test_credential_check_login_then_full_login(self) -> None:
        storage = create_storage(self.config)
        self.addCleanup(storage.close)
        online = storage.online_players
        endpoint = Endpoint(host='10.0.0.9', port=2000)

        login = self._send_raw(
            endpoint, 0, commands.CMD_LOGIN_CLIENT, login_client_payload(b'jill3', NA_TITLE_CODE), FLAG_CHANNEL_BITS
        )
        check = (endpoint, login[0].session_id)
        result = self._send(check, commands.CMD_LOGIN_TO_KICS, bytes(0x128) + bytes(14), FLAG_CHANNEL_BITS)
        self.assertEqual([message.command for message in result], [commands.CMD_RESULT_LOGIN_TO_KICS])
        check = (endpoint, result[0].session_id)
        rows = [row for row in online.list('outbreak2') if row.username == 'jill3']
        self.assertEqual([(row.session_id, row.area_id) for row in rows], [(check[1], 0)])

        self._send(check, commands.CMD_LOGOUT_CLIENT, b'', FLAG_CHANNEL_BITS)
        self.assertFalse([row for row in online.list('outbreak2') if row.username == 'jill3'])

        player = self._login('jill3', '10.0.0.9', 4)
        room_id = self._create_room(player)
        self.assertEqual(self._roster(player, room_id)[player[1]][24:], _profile(4))
        rows = [row for row in online.list('outbreak2') if row.username == 'jill3']
        self.assertEqual([(row.session_id, row.area_id) for row in rows], [(player[1], AREA_ID)])

    # Disband.

    def test_host_disband_closes_the_room(self) -> None:
        host, guest, watcher = self.players
        room_id = self._create_room(host)
        self._join_room(guest, room_id)

        closed = self._send(host, commands.CMD_CHANGE_ATTRIBUTE, b'STAT' + struct.pack('>L', STARTED))
        left = self._send(host, commands.CMD_LEAVE, b'', ROOM_FLAGS)
        deletes = self._to(closed + left, watcher, commands.CMD_DELETE)
        self.assertEqual([message.payload for message in deletes], [struct.pack('>L', room_id)])
        leave = [message for message in self._to(left, guest, commands.CMD_LEAVE)
                 if message.type_flags == FLAG_ROOM | FLAG_RESPONSE]
        self.assertEqual([message.payload for message in leave], [struct.pack('>L', host[1])])

        refused = self._join_room(watcher, room_id)
        self.assertEqual(struct.unpack('>2L', self._to(refused, watcher, commands.CMD_RESULT_ERROR)[0].payload),
                         (commands.CMD_JOIN, 1))
        listing = self._search_rooms(watcher, _room_list_query(AREA_ID))
        self.assertEqual(struct.unpack_from('>3L', listing.payload), (0, 0, 0))

    # Echo.

    def test_echo_is_answered_with_the_same_payload(self) -> None:
        player = self.players[0]
        for type_flags in (ROOM_FLAGS, LOBBY_FLAGS):
            with self.subTest(type_flags=hex(type_flags)):
                reply = self._send(player, commands.CMD_SEND_ECHO, b'\x01', type_flags)
                echoes = self._to(reply, player, commands.CMD_SEND_ECHO)
                self.assertEqual([message.payload for message in echoes], [b'\x01'])


class Outbreak2DirectoryTests(unittest.TestCase):
    def test_areas_are_named_after_the_key(self) -> None:
        directory = build_directory()
        self.assertEqual([area.name for area in directory.areas], [f'AREA{n:02d}' for n in range(1, 11)])
        self.assertEqual([area.area_id for area in directory.areas], list(range(1, 11)))
        self.assertEqual(directory.capacity, 400)
        self.assertEqual(build_directory(area_key='RPD2').areas[-1].name, 'RPD210')

    def test_invalid_settings_are_rejected(self) -> None:
        invalid = (
            {'area_key': 'ARE'},
            {'area_key': 'AREA1'},
            {'area_key': 'AR-A'},
            {'area_key': 'ÁREA'},
            {'server_name': ''},
            {'server_name': 'x' * 16},
            {'server_description': 'Raccoon City ☣'},
            {'capacity': 0},
        )
        for settings in invalid:
            with self.subTest(settings=settings), self.assertRaises(ValueError):
                build_directory(**settings)
        build_directory(server_name='x' * 15)

    def test_capacity_limit_is_a_u16(self) -> None:
        self.assertEqual(build_directory(capacity=65535).capacity, 65535)
        with self.assertRaises(ValueError):
            build_directory(capacity=65536)

    def test_area_texts_must_be_ascii(self) -> None:
        areas = list(DEFAULT_AREAS)
        areas[0] = {**areas[0], 'title': 'Área'}
        with self.assertRaises(ValueError):
            build_directory(areas=tuple(areas))

    def test_rank_kind_names(self) -> None:
        self.assertEqual(build_directory().rank_kind_names(),
                         {0: 'EASY', 1: 'NORMAL', 2: 'HARD', 3: 'VERY HARD', 4: 'ANY'})
        areas = tuple({'title': f'Room {number}', 'rank_kind': number % 2} for number in range(1, 11))
        self.assertEqual(build_directory(areas=areas).rank_kind_names(), {1: 'Room 1', 0: 'Room 2'})

    def test_default_descriptions_use_the_markup(self) -> None:
        for area in build_directory().areas:
            with self.subTest(area=area.name):
                self.assertTrue(area.description.startswith('<BODY>'))
                self.assertTrue(area.description.endswith('<END>'))


if __name__ == '__main__':
    unittest.main()
