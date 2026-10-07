"""Resident Evil Outbreak plugin and APP service tests."""

from dataclasses import replace
from pathlib import Path
import socket
import struct
import tempfile
import unittest

from opensnap.config import StorageConfig, UserConfig, default_app_config
from opensnap.core.engine import SnapProtocolEngine
from opensnap.plugins.outbreak import OutbreakPlugin
from opensnap.plugins.outbreak.directory import build_directory
from opensnap.plugins.outbreak.records import character_name
from opensnap.protocol import commands
from opensnap.protocol.codec import encode_messages
from opensnap.protocol.constants import FLAG_CHANNEL_BITS, FLAG_RELAY, FLAG_RELIABLE, FLAG_RESPONSE, FLAG_ROOM
from opensnap.protocol.models import Endpoint, SnapMessage
from opensnap.storage.sql import SqlAccountStore, SqlRecordStore
from opensnap.storage.sqlite import SqliteConnection
from opensnap_app.capcom.outbreak.netbio import netbio_files, room_rules, scenario_table
from opensnap_app.capcom.outbreak.results import (
    ScenarioResult,
    Teammate,
    decode_scenario_result,
    store_scenario_result,
)
from opensnap_app.capcom.outbreak.service import APP_BUILD, AppService, AppServiceConfig
from opensnap_app.capcom.protocol import (
    APP_DIRECTION_PUSH,
    APP_DIRECTION_REPLY,
    APP_DIRECTION_SERVER,
    app_frame,
    decode_app_field,
    encode_app_field,
    frame_command,
    frame_payload,
    frame_sequence,
    pop_app_frame,
)
from opensnap_app.capcom.server import CapcomAppServer
from tests.support import login_client_payload, serving

OUTBREAK_TITLE_CODE = 0xCAE0
LOBBY_FLAGS = FLAG_CHANNEL_BITS | FLAG_RELIABLE
ROOM_FLAGS = FLAG_ROOM | FLAG_RELIABLE
FREE_HALL_ID = 1
FREE_AREA_ID = 7
STATUS_IN_SCENARIO = 1
STATUS_IN_LOBBY = 0xF7E00001


def _profile(marker: int) -> bytes:
    return bytes([marker]) * 240


def _name_search(name: bytes, operator: int, *more: tuple[bytes, int, bytes]) -> bytes:
    conditions = [(b'NAME', (2 << 5) | operator, name.ljust(16, b'\x00'))] + list(more)
    body = b''.join(key + bytes([flags]) + value for key, flags, value in conditions)
    return struct.pack('>LB3x', 99, len(conditions)) + body


def _create_payload(*, password: bytes = b'', rules: int = 0x00027C02) -> bytes:
    return bytes(16) + struct.pack('>L', 4) + password.ljust(16, b'\x00') + struct.pack('>2L', 1, rules)


def _join_payload(room_id: int, password: bytes = b'') -> bytes:
    return struct.pack('>2L', room_id, 1) + password.ljust(16, b'\x00')


class OutbreakLobbyTests(unittest.TestCase):
    """Areas, rooms and scenarios through the protocol engine."""

    def setUp(self) -> None:
        temp_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temp_directory.cleanup)
        users = tuple(UserConfig(user_id=index + 1, username=f'jill{index}', password='1111') for index in range(4))
        self.config = replace(
            serving(default_app_config(), 'outbreak'),
            storage=StorageConfig(backend='sqlite', sqlite_path=f'{temp_directory.name}/ob.sqlite'),
            users=users,
        )
        self.plugin = OutbreakPlugin()
        self.engine = SnapProtocolEngine(config=self.config, plugin=self.plugin, role='combined')
        self.addCleanup(self.engine.close)
        self.sequences: dict[int, int] = {}
        self.players = [self._login(f'jill{index}', f'10.0.0.{index + 1}', index + 1) for index in range(3)]

    def _login(self, username: str, host: str, marker: int) -> tuple[Endpoint, int]:
        endpoint = Endpoint(host=host, port=2000)
        login = self._send_raw(
            endpoint,
            0,
            commands.CMD_LOGIN_CLIENT,
            login_client_payload(f'{username}\n'.encode(), OUTBREAK_TITLE_CODE),
            FLAG_CHANNEL_BITS,
        )
        session_id = login[0].session_id
        kics = bytes(0x128) + _profile(marker) + bytes(14)
        result = self._send((endpoint, session_id), commands.CMD_LOGIN_TO_KICS, kics, FLAG_CHANNEL_BITS)
        self.assertEqual(result[0].command, commands.CMD_RESULT_LOGIN_TO_KICS)
        player = (endpoint, session_id)
        self._send(player, commands.CMD_CHANGE_USER_STATUS, struct.pack('>L', STATUS_IN_LOBBY))
        self._send(player, commands.CMD_JOIN, struct.pack('>L', FREE_AREA_ID), LOBBY_FLAGS)
        return player

    def _send_raw(self, endpoint, session_id, command, payload, type_flags, sequence=None) -> list[SnapMessage]:
        if sequence is None:
            sequence = self.sequences.get(session_id, 0) + 1
            self.sequences[session_id] = sequence
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
        return result.messages

    def _send(self, player, command, payload=b'', type_flags=ROOM_FLAGS, sequence=None):
        endpoint, session_id = player
        return self._send_raw(endpoint, session_id, command, payload, type_flags, sequence)

    def _create_room(self, player, **kwargs) -> int:
        result = self._send(player, commands.CMD_CREATE_GAME_ROOM, _create_payload(**kwargs), LOBBY_FLAGS)
        wrapper = [message for message in result if message.command == commands.CMD_RESULT_WRAPPER]
        selector, room_id = struct.unpack('>2L', wrapper[0].payload)
        self.assertEqual(selector, commands.CMD_CREATE_GAME_ROOM)
        return room_id

    @staticmethod
    def _to(messages: list[SnapMessage], player, command: int) -> list[SnapMessage]:
        return [message for message in messages if message.session_id == player[1] and message.command == command]

    def test_area_queries_follow_the_client_names(self) -> None:
        player = self.players[0]
        hall = self._send(player, commands.CMD_QUERY_AREA, _name_search(b'obmft', 1), LOBBY_FLAGS)[0]
        self.assertEqual(hall.type_flags, FLAG_CHANNEL_BITS | FLAG_RESPONSE)
        self.assertEqual(struct.unpack_from('>3L', hall.payload), (99, 1, 1))
        name, users, capacity, maximum, _, area_id = struct.unpack_from('>16s5L', hall.payload, 12)
        self.assertEqual((name.rstrip(b'\x00'), users, capacity, maximum, area_id), (b'obmft', 0, 100, 100, 1))

        scenario = self._send(
            player, commands.CMD_QUERY_AREA,
            _name_search(b'obms01', 4, (b'NAME', (2 << 5) | 6, b'obms05'.ljust(16, b'\x00'))), LOBBY_FLAGS,
        )[0]
        self.assertEqual(struct.unpack_from('>L', scenario.payload, 8)[0], 5)

        free = self._send(
            player, commands.CMD_QUERY_AREA,
            _name_search(b'obmf01', 4, (b'NAME', (2 << 5) | 6, b'obmf99'.ljust(16, b'\x00'))), LOBBY_FLAGS,
        )[0]
        self.assertEqual(struct.unpack_from('>L', free.payload, 8)[0], 10)
        first = struct.unpack_from('>16s5L', free.payload, 12)
        self.assertEqual((first[0].rstrip(b'\x00'), first[1], first[5]), (b'obmf01', 3, FREE_AREA_ID))

        oid = struct.pack('>LB3x', 1, 1) + b'OID\x00' + bytes([1]) + struct.pack('>L', FREE_AREA_ID)
        lookup = self._send(player, commands.CMD_QUERY_AREA, oid, LOBBY_FLAGS)[0]
        self.assertEqual(struct.unpack_from('>16s', lookup.payload, 12)[0].rstrip(b'\x00'), b'obmf01')

    def test_attribute_replies_echo_user_and_maxi(self) -> None:
        host = self.players[0]
        room_id = self._create_room(host)
        user = self._send(host, commands.CMD_QUERY_ATTRIBUTE, struct.pack('>L', room_id) + b'USER')[0]
        maxi = self._send(host, commands.CMD_QUERY_ATTRIBUTE, struct.pack('>L', room_id) + b'MAXI')[0]
        self.assertEqual(user.payload, struct.pack('>L4sL', room_id, b'USER', 1))
        self.assertEqual(maxi.payload, struct.pack('>L4sL', room_id, b'MAXI', 4))
        area = self._send(
            host, commands.CMD_QUERY_ATTRIBUTE, struct.pack('>L', FREE_AREA_ID) + b'MAXI', LOBBY_FLAGS
        )[0]
        self.assertEqual(area.payload, struct.pack('>L4sL', FREE_AREA_ID, b'MAXI', 100))

    def test_room_create_list_join_and_roster(self) -> None:
        host, guest, watcher = self.players
        created = self._send(host, commands.CMD_CREATE_GAME_ROOM, _create_payload(), LOBBY_FLAGS)
        room_id = struct.unpack('>2L', self._to(created, host, commands.CMD_RESULT_WRAPPER)[0].payload)[1]
        # Area peers get the new row (slot 4).
        row = self._to(created, watcher, commands.CMD_CREATE_GAME_ROOM)[0].payload
        self.assertEqual(struct.unpack('>16s5L', row)[1:], (1, 0, 0x00027C02, 4, room_id))

        listing = self._send(watcher, commands.CMD_QUERY_GAME_ROOMS, struct.pack('>L', FREE_AREA_ID), LOBBY_FLAGS)[0]
        self.assertEqual(struct.unpack_from('>L', listing.payload, 8)[0], 1)

        joined = self._send(guest, commands.CMD_JOIN, _join_payload(room_id))
        result = self._to(joined, guest, commands.CMD_RESULT_WRAPPER)[0]
        self.assertEqual(struct.unpack('>2L', result.payload), (commands.CMD_JOIN, 0))
        self.assertEqual(result.type_flags, FLAG_ROOM | FLAG_RESPONSE)
        arrival = self._to(joined, host, commands.CMD_JOIN)[0].payload
        self.assertEqual(arrival[:5], b'jill1')
        self.assertEqual(struct.unpack_from('>2L', arrival, 16), (guest[1], 240))
        self.assertEqual(arrival[24:], _profile(2))
        self.assertFalse(self._to(joined, guest, commands.CMD_JOIN))

        self._send(guest, commands.CMD_CHANGE_USER_PROPERTY, _profile(9))
        roster = self._send(host, commands.CMD_QUERY_USER, struct.pack('>L', room_id))[0].payload
        self.assertEqual(struct.unpack_from('>3L', roster), (room_id, 2, 2))
        records = [roster[12:12 + 264], roster[12 + 264:]]
        profiles = {struct.unpack_from('>L', record, 16)[0]: record[24:] for record in records}
        self.assertEqual(profiles, {host[1]: _profile(1), guest[1]: _profile(9)})

    def test_wrong_password_is_error_code_15(self) -> None:
        host, guest, _ = self.players
        room_id = self._create_room(host, password=b'leon')
        refused = self._send(guest, commands.CMD_JOIN, _join_payload(room_id, b'claire'))
        self.assertEqual(refused[-1].command, commands.CMD_RESULT_ERROR)
        self.assertEqual(struct.unpack('>2L', refused[-1].payload), (commands.CMD_JOIN, 15))
        accepted = self._send(guest, commands.CMD_JOIN, _join_payload(room_id, b'leon'))
        self.assertEqual(self._to(accepted, guest, commands.CMD_RESULT_WRAPPER)[0].command, commands.CMD_RESULT_WRAPPER)

    def test_reliable_create_retry_replays_the_same_room(self) -> None:
        host = self.players[0]
        sequence = self.sequences[host[1]] + 1
        self.sequences[host[1]] = sequence
        first = self._send(host, commands.CMD_CREATE_GAME_ROOM, _create_payload(), LOBBY_FLAGS, sequence=sequence)
        retry = self._send(host, commands.CMD_CREATE_GAME_ROOM, _create_payload(), LOBBY_FLAGS, sequence=sequence)
        wrapper = [message.payload for message in first + retry if message.command == commands.CMD_RESULT_WRAPPER]
        self.assertEqual(len(wrapper), 2)
        self.assertEqual(wrapper[0], wrapper[1])
        listing = self._send(host, commands.CMD_QUERY_GAME_ROOMS, struct.pack('>L', FREE_AREA_ID), LOBBY_FLAGS)[0]
        self.assertEqual(struct.unpack_from('>L', listing.payload, 8)[0], 1)

    def test_started_room_leaves_the_list_and_refuses_joins(self) -> None:
        host, guest, watcher = self.players
        room_id = self._create_room(host)
        started = self._send(host, commands.CMD_CHANGE_ATTRIBUTE, b'STAT' + struct.pack('>L', 0x40000000))
        self.assertEqual(self._to(started, watcher, commands.CMD_DELETE)[0].payload, struct.pack('>L', room_id))
        listing = self._send(watcher, commands.CMD_QUERY_GAME_ROOMS, struct.pack('>L', FREE_AREA_ID), LOBBY_FLAGS)[0]
        self.assertEqual(struct.unpack_from('>L', listing.payload, 8)[0], 0)
        refused = self._send(guest, commands.CMD_JOIN, _join_payload(room_id))
        self.assertEqual(refused[-1].command, commands.CMD_RESULT_ERROR)

    def test_no_lobby_callbacks_reach_a_player_in_a_scenario(self) -> None:
        host, guest, watcher = self.players
        room_id = self._create_room(host)
        self._send(guest, commands.CMD_JOIN, _join_payload(room_id))
        self._send(host, commands.CMD_CHANGE_ATTRIBUTE, b'STAT' + struct.pack('>L', 0x40000000))
        for player in (host, guest):
            result = self._send(player, commands.CMD_CHANGE_USER_STATUS, struct.pack('>L', STATUS_IN_SCENARIO))
            self.assertEqual(struct.unpack('>2L', result[0].payload), (commands.CMD_CHANGE_USER_STATUS, 0))

        # Scenario packets are relayed with the sender id and unchanged flags.
        relay = self._send(guest, commands.CMD_SEND, b'\x02\x10', FLAG_ROOM | FLAG_RELIABLE)
        to_host = self._to(relay, host, commands.CMD_SEND)
        self.assertEqual((to_host[0].type_flags, to_host[0].source_session_id), (ROOM_FLAGS, guest[1]))
        self.assertFalse(self._to(relay, guest, commands.CMD_SEND))

        # The guest returns first: its leave and profile reach nobody still playing.
        self._send(guest, commands.CMD_LEAVE)
        quiet = self._send(guest, commands.CMD_CHANGE_USER_PROPERTY, _profile(5))
        quiet += self._send(watcher, commands.CMD_CREATE_GAME_ROOM, _create_payload(), LOBBY_FLAGS)
        self.assertFalse([message for message in quiet if message.session_id == host[1]])

        # Back in the lobby, the host gets lobby callbacks again.
        self._send(host, commands.CMD_CHANGE_USER_STATUS, struct.pack('>L', STATUS_IN_LOBBY))
        created = self._send(guest, commands.CMD_CREATE_GAME_ROOM, _create_payload(), LOBBY_FLAGS)
        self.assertTrue(self._to(created, host, commands.CMD_CREATE_GAME_ROOM))

    def test_chat_reaches_peers_but_not_the_sender(self) -> None:
        host, guest, watcher = self.players
        room_id = self._create_room(host)
        self._send(guest, commands.CMD_JOIN, _join_payload(room_id))
        room_chat = self._send(host, commands.CMD_SEND, b'hello', FLAG_ROOM | FLAG_RELAY | FLAG_RELIABLE)
        self.assertEqual([message.session_id for message in room_chat if message.command == commands.CMD_SEND],
                         [guest[1]])
        hall_chat = self._send(watcher, commands.CMD_SEND, b'anyone?', LOBBY_FLAGS | FLAG_RELAY)
        # Everyone else in the Area is in a room.
        self.assertFalse([message for message in hall_chat if message.command == commands.CMD_SEND])

    def test_friend_search_reports_area_and_room(self) -> None:
        host, guest, _ = self.players
        room_id = self._create_room(host)
        found = self._send(guest, commands.CMD_SEARCH_USERS_BY_NAME, b'jill0-'.ljust(24, b'\x00'), LOBBY_FLAGS)[0]
        self.assertEqual(struct.unpack_from('>L', found.payload, 4)[0], 1)
        self.assertEqual(struct.unpack_from('>3L', found.payload, 12 + 16), (1, FREE_AREA_ID, room_id))
        oid = struct.pack('>LB3x', 1, 1) + b'OID\x00' + bytes([1]) + struct.pack('>L', room_id)
        room = self._send(guest, commands.CMD_SEARCH_ROOMS, oid, LOBBY_FLAGS)[0]
        self.assertEqual(struct.unpack_from('>L', room.payload, 12 + 28)[0], 0x00027C02)


class OutbreakDirectoryTests(unittest.TestCase):
    def test_free_area_count_is_bounded(self) -> None:
        self.assertEqual([area.name for area in build_directory(2, 50).areas][-3:], ['obms05', 'obmf01', 'obmf02'])
        for count in (0, 100):
            with self.assertRaises(ValueError):
                build_directory(count, 50)


class OutbreakAppServiceTests(unittest.TestCase):
    """Both APP connection kinds against a real socket."""

    def setUp(self) -> None:
        temp_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temp_directory.cleanup)
        self.data_directory = Path(temp_directory.name)
        connection = SqliteConnection(self.data_directory / 'shared.sqlite')
        self.addCleanup(connection.close)
        self.accounts = SqlAccountStore(connection)
        self.records = SqlRecordStore(connection)
        self.service = AppService(
            AppServiceConfig(data_directory=self.data_directory), accounts=self.accounts, records=self.records
        )
        server = CapcomAppServer(host='127.0.0.1', port=0, titles={APP_BUILD: self.service})
        server.start()
        self.addCleanup(server.stop)
        self.port = server.address[1]

    def _connect(self) -> tuple[socket.socket, bytearray]:
        client = socket.create_connection(('127.0.0.1', self.port), timeout=5)
        self.addCleanup(client.close)
        return client, bytearray()

    @staticmethod
    def _receive(client: socket.socket, buffer: bytearray) -> bytes:
        while (frame := pop_app_frame(buffer)) is None:
            data = client.recv(65535)
            if not data:
                raise ConnectionError('closed')
            buffer.extend(data)
        return frame

    def _open(self, client: socket.socket, buffer: bytearray) -> None:
        connect = self._receive(client, buffer)
        self.assertEqual((connect[2], frame_command(connect), frame_payload(connect)), (APP_DIRECTION_SERVER, 0x1002, b'\x00\x00'))
        answer = encode_app_field(b'jill0', 0) + b'\x01\x01' + encode_app_field(b'0404141723', 0) + bytes(4)
        client.sendall(app_frame(APP_DIRECTION_REPLY, 0x1002, payload=answer))
        version = self._receive(client, buffer)
        self.assertEqual((version[2], frame_command(version), frame_payload(version)), (APP_DIRECTION_PUSH, 0x6001, b'\x00'))

    def _request(self, client, buffer, command: int, sequence: int, payload: bytes = b'') -> bytes:
        client.sendall(app_frame(1, command, sequence=sequence, payload=payload))
        reply = self._receive(client, buffer)
        self.assertEqual((reply[2], frame_command(reply), frame_sequence(reply)), (APP_DIRECTION_REPLY, command, sequence))
        return frame_payload(reply)

    def test_lobby_entry_serves_the_page_in_requested_chunks(self) -> None:
        (self.data_directory / 'files' / '01').mkdir(parents=True)
        page = b'<BODY><CENTER>' + b'x' * 1500 + b'<END>'
        (self.data_directory / 'files' / '01' / 'TOP_INFOR.HTM').write_bytes(page)
        client, buffer = self._connect()
        self._open(client, buffer)

        name = b'01/TOP_INFOR.HTM'
        info = self._request(client, buffer, 0x6101, 1, bytes(4) + encode_app_field(name, 1))
        echoed, size = decode_app_field(info[5:], 1)
        self.assertEqual((info[0], echoed), (1, name))
        total = struct.unpack_from('>L', info, 5 + size)[0]
        self.assertEqual(total, len(page) + 1)

        received = b''
        sequence = 2
        while len(received) < total:
            chunk = min(766 - (len(name) + 12), total - len(received))
            request = encode_app_field(name, sequence) + struct.pack('>LH', len(received), chunk)
            reply = self._request(client, buffer, 0x6102, sequence, request)
            echoed, size = decode_app_field(reply, sequence)
            self.assertEqual(struct.unpack_from('>L', reply, size)[0], len(received))
            data, _ = decode_app_field(reply[size + 4:], sequence)
            self.assertEqual(len(data), chunk)
            received += data
            sequence += 1
        self.assertEqual(received, page + b'\x00')

        self.assertEqual(self._request(client, buffer, 0x6401, sequence), b'\x00\x00')
        self.assertEqual(self._download_data_files(client, buffer, sequence + 1), list(netbio_files()))
        self.assertEqual(self._request(client, buffer, 0x1004, 100), b'')

    def _download_data_files(self, client, buffer, sequence: int) -> list[bytes]:
        """Fetch the data files like the client does: 6201 for their sizes, then 754-byte 6202 chunks."""

        info = self._request(client, buffer, 0x6201, sequence, encode_app_field(b'', sequence))
        name, size = decode_app_field(info[1:], sequence)
        self.assertEqual((info[0], name), (1, b'BASLUS-20765NETBIO'.ljust(31, b'\x00')))
        count = struct.unpack_from('>H', info, 1 + size)[0]
        files = []
        for index, total in enumerate(struct.unpack_from(f'>{count}L', info, 3 + size)):
            received = b''
            while len(received) < total:
                sequence += 1
                reply = self._request(client, buffer, 0x6202, sequence, struct.pack('>HLH', index, len(received), 754))
                self.assertEqual(struct.unpack_from('>HL', reply), (index, len(received)))
                received += decode_app_field(reply[6:], sequence)[0]
            files.append(received)
        return files

    def test_database_menu_pages_are_the_rankings(self) -> None:
        # DATABASE opens `lbs://lbs/01/DATABASE.HTM`; the browser fetches it as `01/DATABASE.HTM`.
        account = self.accounts.create_user('jill0', 'secret')
        store_scenario_result(
            self.records, account,
            ScenarioResult(3, False, True, 6, 23339, 15525, (), 32, 8, 0, 0, 286, 2027),
        )
        client, buffer = self._connect()
        self._open(client, buffer)
        index = self._download(client, buffer, b'01/DATABASE.HTM', 1)
        self.assertIn(b'<a href="lbs://lbs/01/RANK_CLEAR_3.HTM">Fastest clears: Hellfire</a>', index)
        self.assertIn(b'<a href="lbs://lbs/01/RANK_POINTS.HTM">', index)
        clears = self._download(client, buffer, b'01/RANK_CLEAR_3.HTM', 10)
        self.assertIn(b'<td>jill0</td><td>YOKO</td><td>12\'57"</td>', clears)
        self.assertIn(b'<a href="lbs://lbs/01/DATABASE.HTM">Database</a>', clears)
        points = self._download(client, buffer, b'01/RANK_POINTS.HTM', 20)
        self.assertIn(b'<td>jill0</td><td>15525</td>', points)

    def _download(self, client, buffer, name: bytes, sequence: int) -> bytes:
        """Fetch one page like the client does: 6101 for its size, then 6102 chunks."""

        info = self._request(client, buffer, 0x6101, sequence, bytes(4) + encode_app_field(name, sequence))
        _, size = decode_app_field(info[5:], sequence)
        total = struct.unpack_from('>L', info, 5 + size)[0]
        received = b''
        while len(received) < total:
            sequence += 1
            chunk = min(766 - (len(name) + 12), total - len(received))
            request = encode_app_field(name, sequence) + struct.pack('>LH', len(received), chunk)
            reply = self._request(client, buffer, 0x6102, sequence, request)
            _, size = decode_app_field(reply, sequence)
            received += decode_app_field(reply[size + 4:], sequence)[0]
        return received

    def test_missing_page_is_published_empty(self) -> None:
        client, buffer = self._connect()
        self._open(client, buffer)
        info = self._request(client, buffer, 0x6101, 1, bytes(4) + encode_app_field(b'01/TOP_INFOR.HTM', 1))
        _, size = decode_app_field(info[5:], 1)
        # Size 0 would skip to a step openSNAP does not serve; an empty page is just its NUL.
        self.assertEqual(struct.unpack_from('>L', info, 5 + size)[0], 1)

    def test_shipped_information_page_matches_the_other_games(self) -> None:
        outbreak = Path('data/outbreak/files/01/TOP_INFOR.HTM').read_text()
        monster_hunter = Path('data/monsterhunter/files/02/TOP_INFOR.HTM').read_text()
        self.assertEqual(outbreak, monster_hunter.replace('Monster Hunter 1', 'Resident Evil Outbreak'))

    def test_scenario_result_report_is_stored(self) -> None:
        account = self.accounts.create_user('jill0', 'secret')
        client, buffer = self._connect()
        self._open(client, buffer)
        report = _scenario_report(
            3, sequence=1, clear_ticks=23339, points=15525, teammates=((b'kevin', 1, 0), (b'yoko', 2, 124))
        )
        self.assertEqual(self._request(client, buffer, 0x6301, 1, report), b'')
        self.assertEqual(self._request(client, buffer, 0x1004, 2), b'')

        boards = self.records.best_by_board('outbreak', 10)
        team = [{'name': 'kevin', 'alive': True, 'character': 0}, {'name': 'yoko', 'alive': False, 'character': 124}]
        [points] = boards['points-3']
        self.assertEqual((points.user_id, points.player, points.score), (account.user_id, 'jill0', 15525))
        self.assertEqual(
            dict(points.details),
            {
                'free_mode': True, 'cleared': True, 'clear_ticks': 23339, 'character': 3, 'teammates': team,
                'echo_ticks': 32, 'echo_replies': 8, 'echo_losses': 0, 'network_error': 0,
                'peak_queue': 286, 'peak_location': 2027,
            },
        )
        [clear] = boards['clear-3-1']
        self.assertEqual((clear.player, clear.score, dict(clear.details)), ('jill0', 23339, {'character': 3, 'teammates': team}))

    def test_failed_scenario_is_kept_off_the_clear_board(self) -> None:
        self.accounts.create_user('jill0', 'secret')
        client, buffer = self._connect()
        self._open(client, buffer)
        self.assertEqual(self._request(client, buffer, 0x6301, 1, _scenario_report(2, sequence=1, clear_ticks=0, points=614)), b'')
        self.assertEqual(self._request(client, buffer, 0x1004, 2), b'')
        boards = self.records.best_by_board('outbreak', 10)
        self.assertEqual((set(boards), boards['points-2'][0].score), ({'points-2'}, 614))

    def test_scenario_result_of_an_unknown_login_is_acknowledged_only(self) -> None:
        client, buffer = self._connect()
        self._open(client, buffer)
        self.assertEqual(self._request(client, buffer, 0x6301, 1, _scenario_report(0, sequence=1)), b'')
        self.assertEqual(self.records.best_by_board('outbreak', 10), {})


class OutbreakNetbioTests(unittest.TestCase):
    """The data files, read the way `netwk.bin` reads them."""

    def test_files_fill_the_client_slots(self) -> None:
        self.assertEqual([len(data) for data in netbio_files()], [0x2000, 0x2000])

    def test_scenario_records_list_the_five_scenarios(self) -> None:
        table = scenario_table()

        def record(index: int) -> bytes:
            return table[index * 72:index * 72 + 72]

        def description(index: int) -> bytes:
            start = 0xE08 + struct.unpack_from('<i', record(index), 68)[0]
            return table[start:table.index(b'\x00', start)]

        self.assertEqual(
            [record(index)[3:35].rstrip(b'\x00') for index in range(5)],
            [b'Outbreak', b'Below Freezing Point', b'The Hive', b'Hellfire', b'Decisions, Decisions'],
        )
        self.assertEqual(record(1)[:3], b'\x02\x01\x02')
        self.assertEqual(record(0)[35:46], b'\x09\x02\x03\x04\x02\x03\x04\x02\x03\x04\x00')
        self.assertEqual(record(0)[52:55], b'\x02\x05\x05')
        self.assertEqual(
            description(0),
            b"<BODY>It was a typical night at J's bar.<BR><BODY>Some uninvited guests crashed the party."
            b'<BR><BODY>Our race for survival was just beginning.<END>',
        )
        self.assertTrue(description(4).startswith(b'<BODY>Destruction. Darkness.'))
        self.assertEqual({struct.unpack_from('<i', record(index), 68)[0] for index in range(5, 48)}, {-1})
        self.assertEqual(table[0xD80:0xD84], b'\x00\x06\x1f\x01')
        self.assertEqual(table[0xDA8:0xDC8].rstrip(b'\x00'), b'RESIDENT EVIL OUTBREAK')

    def test_room_rules_match_the_manual(self) -> None:
        self.assertEqual(
            room_rules()[:22],
            b'\x01\x01\x01\x01' b'\x00\x00\x01\x01' b'\x00\x00\x00\x00' b'\x00\x00\x00' b'\x01\x00\x00' b'\x78\x00' b'\x01\x01',
        )
        self.assertFalse(any(room_rules()[22:]))


class OutbreakScenarioResultTests(unittest.TestCase):
    def test_every_field_is_decoded(self) -> None:
        report = _scenario_report(
            4, sequence=5, free_mode=False, character=38, clear_ticks=94260, points=3780,
            teammates=((b'kevin', 2, 99), (b'mark', 1, 7)), network=(29, 871, 7, 1, 302, 41057),
        )
        result = decode_scenario_result(report, 5)
        self.assertEqual(
            (result.scenario, result.free_mode, result.cleared, result.character, result.clear_ticks, result.points),
            (4, False, True, 38, 94260, 3780),
        )
        self.assertEqual(result.teammates, (Teammate('kevin', False, 99), Teammate('mark', True, 7)))
        self.assertEqual(
            (result.echo_ticks, result.network_error, result.echo_replies, result.echo_losses,
             result.peak_queue, result.peak_location),
            (29, 871, 7, 1, 302, 41057),
        )

    def test_failed_scenario_has_no_clear_time(self) -> None:
        result = decode_scenario_result(_scenario_report(2, sequence=5, clear_ticks=0, points=614), 5)
        self.assertEqual((result.scenario, result.cleared, result.clear_ticks, result.points), (2, False, 0, 614))
        self.assertEqual(result.teammates, ())

    def test_report_must_have_its_exact_size(self) -> None:
        report = _scenario_report(1, sequence=5)
        for broken in (report[:-1], report + b'\x00', report[:2]):
            with self.assertRaises(ValueError):
                decode_scenario_result(broken, 5)

    def test_character_codes_name_main_characters_and_npcs(self) -> None:
        # Main characters are 0..7; an NPC is its id + 9 (`U.S.S.2` is NPC 115, `YOKO:D` NPC 90).
        self.assertEqual(
            [character_name(code) for code in (0, 7, 8, 99, 124, 255)],
            ['KEVIN', 'CINDY', '?', 'YOKO:D', 'U.S.S.2', '?'],
        )


def _scenario_report(
    scenario: int,
    *,
    sequence: int,
    free_mode: bool = True,
    character: int = 3,
    clear_ticks: int = 36000,
    points: int = 4000,
    teammates: tuple[tuple[bytes, int, int], ...] = (),
    network: tuple[int, ...] = (32, 0, 8, 0, 286, 2027),
) -> bytes:
    """A 6301 payload as `netwk.bin` `0x0059ba40` builds it; a zero clear time means no clear.

    `teammates` are (name, status 1 alive / 2 not, character); `network` the echo time, network
    error, echo replies and losses, peak queue and its location.
    """

    cleared = int(clear_ticks != 0)
    slots = [*teammates, *([(b'', 0, 0)] * (3 - len(teammates)))]
    return (
        bytes([cleared, int(free_mode), scenario])
        + encode_app_field(b'jill0\x00', sequence)
        + bytes([cleared, character])
        + struct.pack('>LL', clear_ticks, points)
        + b''.join(encode_app_field(name + b'\x00', sequence) + bytes([status, code]) for name, status, code in slots)
        + struct.pack('>8L', *network, 0, 0)
    )

if __name__ == '__main__':
    unittest.main()
