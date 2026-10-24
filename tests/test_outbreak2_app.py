"""Resident Evil Outbreak File#2 APP service, data file and Area settings tests."""

import json
import os
from pathlib import Path
import socket
import struct
import tempfile
import unittest
from unittest import mock

from opensnap import config as opensnap_config
from opensnap.core.sessions import Session
from opensnap.plugins.outbreak2.directory import DEFAULT_AREAS, build_directory, read_directory
from opensnap.protocol.models import Endpoint
from opensnap.storage.sql import SqlAccountStore, SqlOnlinePlayerStore, SqlRecordStore
from opensnap.storage.sqlite import SqliteConnection
from opensnap_app.capcom.outbreak2.netbio import data_files, data_token
from opensnap_app.capcom.outbreak2.results import store_scenario_result
from opensnap_app.capcom.outbreak2.service import (
    APP_BUILD_EU,
    APP_BUILD_NA,
    BROWSER_PAGE_LIMIT,
    INFORMATION_PAGE_LIMIT,
    AppService,
    AppServiceConfig,
    read_app_service_config,
)
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
from opensnap_app.capcom.outbreak.scenario_report import ScenarioResult
from opensnap_app.capcom.server import CapcomAppServer

REGISTRATION_URL = b'http://regweb.reo.capcom.sf.yav4.com/reweb/signup/'
# The bootstrap address the 6503 World key carries; the lobby logs into it.
BOOTSTRAP_HOST = '192.0.2.10'
# The client's 6102 and 6204 chunk sizes (`0x001faa70`, `0x001f9740`).
PAGE_CHUNK = 722
DATA_CHUNK = 754
EU_LANGUAGE = 1
SCENARIO_NAMES = (
    'Wild Things', 'Underbelly', 'Flashback', 'Desperate Times', 'End of the Road',
    'Elimination 1', 'Elimination 2', 'Elimination 3', 'Showdown 1', 'Showdown 2', 'Showdown 3',
)


def _session(session_id: int) -> Session:
    return Session(
        session_id=session_id, user_id=session_id, username=f'p{session_id}', endpoint=Endpoint('10.0.0.1', 4000)
    )


class Outbreak2AppServiceTests(unittest.TestCase):
    """Every File#2 APP connection kind against a real socket."""

    def setUp(self) -> None:
        temp_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temp_directory.cleanup)
        self.data_directory = Path(temp_directory.name)
        connection = SqliteConnection(self.data_directory / 'shared.sqlite')
        self.addCleanup(connection.close)
        self.accounts = SqlAccountStore(connection, server_secret='test secret')
        self.records = SqlRecordStore(connection)
        self.online_players = SqlOnlinePlayerStore(connection)
        self.directory = build_directory()
        self.service = self._serve(AppServiceConfig(self.data_directory, bootstrap_advertise_host=BOOTSTRAP_HOST))

    def _serve(self, config: AppServiceConfig) -> AppService:
        """Start a File#2 APP service on a free port; `self.port` reaches the last one started."""

        service = AppService(config, self.directory, self.accounts, self.records, self.online_players)
        server = CapcomAppServer(host='127.0.0.1', port=0, titles={APP_BUILD_NA: service, APP_BUILD_EU: service})
        server.start()
        self.addCleanup(server.stop)
        self.port = server.address[1]
        return service

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

    def _open(self, *, eu: bool = False) -> tuple[socket.socket, bytearray]:
        """Connect and answer the 1002 as NA (build 4, flags 1) or EU (build 6, flags `language | 0x10`)."""

        client, buffer = self._connect()
        connect = self._receive(client, buffer)
        self.assertEqual(
            (connect[2], frame_command(connect), frame_payload(connect)), (APP_DIRECTION_SERVER, 0x1002, b'\x00\x00')
        )
        build, flags = (APP_BUILD_EU, EU_LANGUAGE | 0x10) if eu else (APP_BUILD_NA, 1)
        answer = encode_app_field(b'jill0', 0) + bytes([build, flags]) + encode_app_field(b'0508021200', 0) + bytes(4)
        client.sendall(app_frame(APP_DIRECTION_REPLY, 0x1002, payload=answer))
        version = self._receive(client, buffer)
        self.assertEqual(
            (version[2], frame_command(version), frame_payload(version)), (APP_DIRECTION_PUSH, 0x6001, b'\x00')
        )
        return client, buffer

    def _request(self, client, buffer, command: int, sequence: int, payload: bytes = b'') -> bytes:
        client.sendall(app_frame(1, command, sequence=sequence, payload=payload))
        reply = self._receive(client, buffer)
        self.assertEqual(
            (reply[2], frame_command(reply), frame_sequence(reply)), (APP_DIRECTION_REPLY, command, sequence)
        )
        return frame_payload(reply)

    def _download(self, client, buffer, name: bytes, sequence: int) -> tuple[bytes, int]:
        """Fetch one page like the client: 6101 for its size, then 722-byte 6102 chunks. Returns the next sequence."""

        info = self._request(client, buffer, 0x6101, sequence, bytes(4) + encode_app_field(name, sequence))
        self.assertEqual(info[:5], b'\x01' + bytes(4))
        echoed, size = decode_app_field(info[5:], sequence)
        self.assertEqual(echoed, name)
        self.assertEqual(len(info), 5 + size + 4)
        total = struct.unpack_from('>L', info, 5 + size)[0]
        received = b''
        while len(received) < total:
            sequence += 1
            request = encode_app_field(name, sequence) + struct.pack('>LH', len(received), PAGE_CHUNK)
            reply = self._request(client, buffer, 0x6102, sequence, request)
            echoed, size = decode_app_field(reply, sequence)
            self.assertEqual((echoed, struct.unpack_from('>L', reply, size)[0]), (name, len(received)))
            data, data_size = decode_app_field(reply[size + 4:], sequence)
            self.assertEqual(len(reply), size + 4 + data_size)
            self.assertLessEqual(len(data), PAGE_CHUNK)
            received += data
        self.assertEqual(len(received), total)
        return received, sequence + 1

    def _download_data_files(self, client, buffer, sequence: int, token: bytes = b'') -> tuple[list[bytes], int]:
        """6203 for the token and sizes, then 754-byte 6204 chunks. Returns the files and the next sequence."""

        info = self._request(client, buffer, 0x6203, sequence, encode_app_field(token, sequence))
        name, size = decode_app_field(info[1:], sequence)
        self.assertEqual((info[0], name), (1, data_token(self.directory)))
        count = struct.unpack_from('>H', info, 1 + size)[0]
        sizes = struct.unpack_from(f'>{count}L', info, 3 + size)
        self.assertEqual(len(info), 3 + size + 4 * count)
        self.assertEqual(sizes, (6012, 16))
        files = []
        for index, total in enumerate(sizes):
            received = b''
            while len(received) < total:
                sequence += 1
                reply = self._request(client, buffer, 0x6204, sequence, struct.pack('>HLH', index, len(received), DATA_CHUNK))
                self.assertEqual(struct.unpack_from('>HL', reply), (index, len(received)))
                data, data_size = decode_app_field(reply[6:], sequence)
                self.assertEqual(len(reply), 6 + data_size)
                self.assertLessEqual(len(data), DATA_CHUNK)
                received += data
            files.append(received)
        return files, sequence + 1

    def _write_page(self, name: str, content: bytes) -> None:
        path = self.data_directory / 'files' / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

    # Information connection (mode 0).

    def test_information_connection_na(self) -> None:
        page = b'<BODY><CENTER>' + b'x' * 1500 + b'<END>'
        self._write_page('04/TOP_INFOR.HTM', page)
        client, buffer = self._open()

        self.assertEqual(
            self._request(client, buffer, 0x6110, 1),
            b'\x00\x00' + encode_app_field(REGISTRATION_URL, 1) + encode_app_field(b'', 1),
        )
        self.assertEqual(self._request(client, buffer, 0x6210, 2), bytes(68))
        received, sequence = self._download(client, buffer, b'04/TOP_INFOR.HTM', 3)
        self.assertEqual(received, page + b'\x00')
        for command in (0x6401, 0x6403, 0x6105):
            self.assertEqual(self._request(client, buffer, command, sequence), b'\x00\x00')
            sequence += 1
        self.assertEqual(self._request(client, buffer, 0x1004, sequence), b'')

    def test_information_connection_eu(self) -> None:
        page = b'<BODY>European news<END>'
        self._write_page('06/01/TOP_INFOR.HTM', page)
        client, buffer = self._open(eu=True)

        self.assertEqual(
            self._request(client, buffer, 0x6110, 1),
            b'\x00\x00' + encode_app_field(REGISTRATION_URL, 1) + encode_app_field(b'', 1),
        )
        self.assertEqual(self._request(client, buffer, 0x6220, 2), b'\x01' + bytes(4))
        self.assertEqual(self._request(client, buffer, 0x6210, 3), bytes(68))
        received, sequence = self._download(client, buffer, b'06/01/TOP_INFOR.HTM', 4)
        self.assertEqual(received, page + b'\x00')
        self.assertEqual(self._request(client, buffer, 0x1004, sequence), b'')

    def test_missing_page_is_a_single_nul(self) -> None:
        client, buffer = self._open()
        received, _ = self._download(client, buffer, b'04/TOP_INFOR.HTM', 1)
        self.assertEqual(received, b'\x00')

    def test_page_chunk_never_exceeds_the_requested_size(self) -> None:
        page = b'y' * 1000
        self._write_page('04/TOP_INFOR.HTM', page)
        client, buffer = self._open()
        name = b'04/TOP_INFOR.HTM'
        self._request(client, buffer, 0x6101, 1, bytes(4) + encode_app_field(name, 1))
        for sequence, (offset, chunk, expected) in enumerate(
            ((0, 100, 100), (100, 1, 1), (950, 722, 51)), start=2
        ):
            request = encode_app_field(name, sequence) + struct.pack('>LH', offset, chunk)
            reply = self._request(client, buffer, 0x6102, sequence, request)
            _, size = decode_app_field(reply, sequence)
            data, _ = decode_app_field(reply[size + 4:], sequence)
            self.assertEqual(len(data), expected)
            self.assertEqual(data, (page + b'\x00')[offset:offset + expected])

    def test_information_page_is_cut_at_4096_bytes(self) -> None:
        self._write_page('04/TOP_INFOR.HTM', b'z' * 5000)
        client, buffer = self._open()
        received, _ = self._download(client, buffer, b'04/TOP_INFOR.HTM', 1)
        self.assertEqual(len(received), 4096)
        self.assertEqual(received, b'z' * 4095 + b'\x00')

    def test_page_limits(self) -> None:
        self.assertEqual(INFORMATION_PAGE_LIMIT, 4096)
        self.assertEqual(BROWSER_PAGE_LIMIT, 0x8000)

    # Login connection (mode 1).

    def test_login_connection(self) -> None:
        client, buffer = self._open()
        key = BOOTSTRAP_HOST.encode('ascii')
        name = self.directory.server_name.encode('ascii')
        description = self.directory.server_description.encode('ascii')

        self.assertEqual(self._request(client, buffer, 0x1007, 1), b'\x00')
        self.assertEqual(self._request(client, buffer, 0x6103, 2), b'\x00\x00')
        self.assertEqual(self._request(client, buffer, 0x6212, 3), bytes(4))

        token = data_token(self.directory)
        self.assertEqual(len(token), 29)
        self.assertTrue(token.startswith(b'OPENSNAP-'))
        files, sequence = self._download_data_files(client, buffer, 4)
        self.assertEqual(tuple(files), data_files(self.directory))

        self.assertEqual(self._request(client, buffer, 0x6211, sequence), bytes(4))
        sequence += 1
        self.assertEqual(
            self._request(client, buffer, 0x6213, sequence), struct.pack('>8H', 20, 300, 300, 5400, 1800, 1800, 1800, 1800)
        )
        sequence += 1
        self.assertEqual(self._request(client, buffer, 0x6501, sequence, bytes(4)), b'\x01' + struct.pack('>2L', 0, 1))
        sequence += 1

        worlds = self._request(client, buffer, 0x6503, sequence, struct.pack('>2H', 0, 8))
        self.assertEqual(
            worlds,
            struct.pack('>2HB', 1, 0, 1)
            + encode_app_field(key, sequence)
            + encode_app_field(name, sequence)
            + bytes(8)
            + encode_app_field(description, sequence),
        )
        sequence += 1
        self.assertEqual(
            self._request(client, buffer, 0x6503, sequence, struct.pack('>2H', 1, 8)), struct.pack('>2HB', 1, 1, 0)
        )
        sequence += 1

        self.assertEqual(
            self._request(client, buffer, 0x6502, sequence, bytes(4) + encode_app_field(key, sequence)),
            b'\x01' + bytes(8),
        )
        sequence += 1

        lands = self._request(client, buffer, 0x6504, sequence, struct.pack('>2H', 0, 8) + encode_app_field(key, sequence))
        self.assertEqual(
            lands,
            struct.pack('>2HB', 1, 0, 1)
            + encode_app_field(b'AREA', sequence)
            + encode_app_field(name, sequence)
            + bytes(8)
            + encode_app_field(description, sequence)
            + struct.pack('>2HL', 10, 400, 0),
        )
        sequence += 1

        self.assertEqual(self._land_population(client, buffer, sequence, key), 0)
        self.assertEqual(self._request(client, buffer, 0x1004, sequence + 1), b'')

    def _land_population(self, client, buffer, sequence: int, world: bytes, land: bytes = b'AREA') -> int:
        world_field = encode_app_field(world, sequence)
        reply = self._request(
            client, buffer, 0x6510, sequence, b'\x01' + encode_app_field(land, sequence) + world_field
        )
        self.assertEqual(reply[0], 1)
        self.assertEqual(reply[3:], world_field)
        return struct.unpack_from('>H', reply, 1)[0]

    def test_land_population_counts_players_in_areas(self) -> None:
        for session_id, area_id in ((1, 1), (2, 1), (3, 7), (4, 0)):
            self.online_players.login('outbreak2', _session(session_id))
            self.online_players.set_area(session_id, area_id)
        # Players of another game never count.
        self.online_players.login('outbreak', _session(5))
        self.online_players.set_area(5, 3)

        client, buffer = self._open()
        key = BOOTSTRAP_HOST.encode('ascii')
        self.assertEqual(self._land_population(client, buffer, 1, key), 3)
        self.assertEqual(self._land_population(client, buffer, 2, key, land=b'NOPE'), 0)

    def test_world_key_defaults_to_this_machine_as_the_client_reaches_it(self) -> None:
        """No advertised host: the key is the local address routed towards the client (here the loopback)."""

        self._serve(AppServiceConfig(self.data_directory))
        client, buffer = self._open()
        worlds = self._request(client, buffer, 0x6503, 1, struct.pack('>2H', 0, 8))
        self.assertEqual(decode_app_field(worlds[5:], 1)[0].rstrip(b'\x00'), b'127.0.0.1')

    def test_config_reader_takes_the_bootstrap_endpoint(self) -> None:
        environment = {'OPENSNAP_BOOTSTRAP_HOST': '10.1.2.3', 'OPENSNAP_BOOTSTRAP_ADVERTISE_HOST': '203.0.113.9'}
        with mock.patch.dict(os.environ, environment), mock.patch.object(opensnap_config, 'load_env_file'):
            config = read_app_service_config(opensnap_config.default_app_config())
        self.assertEqual((config.bootstrap_host, config.bootstrap_advertise_host), ('10.1.2.3', '203.0.113.9'))
        self.assertEqual(config.data_directory, Path('data') / 'outbreak2')

    # Population refresh (mode 5).

    def test_population_refresh_skips_the_server_list(self) -> None:
        self.online_players.login('outbreak2', _session(1))
        self.online_players.set_area(1, 4)
        client, buffer = self._open()
        key = BOOTSTRAP_HOST.encode('ascii')
        # The login's 6501 reply hands out the token the refresh sends back (`0x001fb7d4`).
        login_reply = self._request(client, buffer, 0x6501, 1, struct.pack('>L', 0))
        self.assertEqual(login_reply, b'\x01' + struct.pack('>2L', 0, 1))
        self.assertEqual(self._request(client, buffer, 0x6501, 1, login_reply[5:9]), b'\x00')
        self.assertEqual(self._land_population(client, buffer, 2, key), 1)
        self.assertEqual(self._request(client, buffer, 0x1004, 3), b'')

    # Result connection (mode 2).

    def test_scenario_result_connection_stores_the_report(self) -> None:
        account = self.accounts.create_user('jill0', 'secret')
        client, buffer = self._open()
        token = data_token(self.directory)

        # A client holding the server's token gets the same name and skips the download.
        info = self._request(client, buffer, 0x6203, 1, encode_app_field(token, 1))
        name, size = decode_app_field(info[1:], 1)
        self.assertEqual((info[0], name), (1, token))
        self.assertEqual(self._request(client, buffer, 0x6211, 2), bytes(4))

        report = _scenario_report(
            3, sequence=3, rank_kind=2, character=57, clear_ticks=23339, points=15525,
            teammates=((b'kevin', 1, 0), (b'yoko', 2, 6)), peak_messages=41, flags=0x80000001,
        )
        self.assertEqual(self._request(client, buffer, 0x6301, 3, report), b'')
        self.assertEqual(self._request(client, buffer, 0x1004, 4), b'')

        self.assertEqual(self.records.boards('outbreak2'), ['clear-3-2', 'points'])
        team = [{'name': 'kevin', 'alive': True, 'character': 0}, {'name': 'yoko', 'alive': False, 'character': 6}]
        [points] = self.records.top('outbreak2', 'points', 10)
        self.assertEqual((points.user_id, points.player, points.score), (account.user_id, 'jill0', 15525))
        [clear] = self.records.top('outbreak2', 'clear-3-2', 10)
        self.assertEqual(
            (clear.player, clear.score, dict(clear.details)), ('jill0', 23339, {'character': 57, 'teammates': team})
        )

    def test_failed_scenario_is_only_on_the_points_board(self) -> None:
        self.accounts.create_user('jill0', 'secret')
        client, buffer = self._open()
        report = _scenario_report(5, sequence=1, rank_kind=4, clear_ticks=0, points=614)
        self.assertEqual(self._request(client, buffer, 0x6301, 1, report), b'')
        self.assertEqual(self._request(client, buffer, 0x1004, 2), b'')
        self.assertEqual(self.records.boards('outbreak2'), ['points'])
        self.assertEqual(self.records.top('outbreak2', 'points', 10)[0].score, 614)

    def test_scenario_result_of_an_unknown_login_is_acknowledged_only(self) -> None:
        client, buffer = self._open()
        self.assertEqual(self._request(client, buffer, 0x6301, 1, _scenario_report(0, sequence=1)), b'')
        self.assertEqual(self._request(client, buffer, 0x1004, 2), b'')
        self.assertEqual(self.records.boards('outbreak2'), [])

    # Browser pages (mode 3).

    def test_database_index_links_the_file2_rankings(self) -> None:
        client, buffer = self._open()
        index, sequence = self._download(client, buffer, b'04/DATABASE.HTM', 1)
        for number, name in enumerate(SCENARIO_NAMES):
            self.assertIn(
                f'<a href="lbs://lbs/04/RANK_CLEAR_{number}.HTM">Fastest clears: {name}</a>'.encode(), index
            )
        self.assertIn(b'<a href="lbs://lbs/04/RANK_POINTS.HTM">', index)
        self.assertTrue(index.endswith(b'\x00'))

        eu_index, _ = self._download(client, buffer, b'06/02/DATABASE.HTM', sequence)
        self.assertIn(b'<a href="lbs://lbs/06/02/RANK_CLEAR_10.HTM">Fastest clears: Showdown 3</a>', eu_index)
        self.assertIn(b'<a href="lbs://lbs/06/02/RANK_POINTS.HTM">', eu_index)
        self.assertNotIn(b'lbs://lbs/04/', eu_index)

    def test_clear_pages_show_only_used_rank_kinds(self) -> None:
        jill = self.accounts.create_user('jill0', 'secret')
        carlos = self.accounts.create_user('carlos', 'secret')
        store_scenario_result(self.records, jill, _result(0, rank_kind=0, character=6, clear_ticks=23339, points=900))
        store_scenario_result(self.records, carlos, _result(2, rank_kind=4, character=57, clear_ticks=3000, points=100))
        client, buffer = self._open()

        wild_things, sequence = self._download(client, buffer, b'04/RANK_CLEAR_0.HTM', 1)
        self.assertIn(b'<td>1</td><td>jill0</td><td>Yoko</td><td>12\'57"</td>', wild_things)
        self.assertEqual(wild_things.count(b'Wild Things: '), 1)
        self.assertIn(b'>Wild Things: EASY<', wild_things)
        self.assertNotIn(b'No clears yet', wild_things)
        self.assertIn(b'<a href="lbs://lbs/04/DATABASE.HTM">Database</a>', wild_things)

        flashback, sequence = self._download(client, buffer, b'04/RANK_CLEAR_2.HTM', sequence)
        self.assertIn(b'<td>1</td><td>carlos</td><td>Axeman</td><td>1\'40"</td>', flashback)
        self.assertEqual(flashback.count(b'Flashback: '), 1)
        self.assertIn(b'>Flashback: ANY<', flashback)

        underbelly, sequence = self._download(client, buffer, b'04/RANK_CLEAR_1.HTM', sequence)
        self.assertEqual(underbelly.count(b'No clears yet'), 1)
        self.assertEqual(underbelly.count(b'Underbelly: '), 1)

        points, sequence = self._download(client, buffer, b'04/RANK_POINTS.HTM', sequence)
        self.assertIn(b'<td>jill0</td><td>900</td>', points)
        self.assertIn(b'<td>carlos</td><td>100</td>', points)

        unknown, _ = self._download(client, buffer, b'04/RANK_CLEAR_11.HTM', sequence)
        self.assertEqual(unknown, b'\x00')


class Outbreak2NetbioTests(unittest.TestCase):
    """The data files, read the way `netwk.bin` reads them (see `outbreak2.netbio`)."""

    def setUp(self) -> None:
        self.directory = build_directory()
        self.table = data_files(self.directory)[0]

    def _record(self, index: int) -> bytes:
        return self.table[index * 484:(index + 1) * 484]

    def test_file_sizes(self) -> None:
        self.assertEqual([len(data) for data in data_files(self.directory)], [6012, 16])
        self.assertEqual(data_files(self.directory)[1], bytes(16))

    def test_area_records(self) -> None:
        titles = [area.title for area in self.directory.areas]
        for index, area in enumerate(self.directory.areas):
            record = self._record(index)
            with self.subTest(area=area.number):
                self.assertEqual(record[3], 0xFF)
                self.assertEqual(struct.unpack_from('<2L', record, 20), (0x7FF, 0x1F))
                self.assertEqual(record[28:52].rstrip(b'\x00').decode(), titles[index])
                self.assertEqual(record[52:76].rstrip(b'\x00').decode(), area.subtitle)
                description = record[76:332]
                self.assertIn(b'\x00', description)
                self.assertEqual(description.split(b'\x00', 1)[0].decode(), area.description)
                self.assertEqual((record[332], record[363]), (3, 3))
                difficulty = 1 if area.fixed_difficulty is None else area.fixed_difficulty
                adjustable = int(area.fixed_difficulty is None)
                self.assertEqual(
                    record[364:364 + 19],
                    b'\xf7\xe1\x04'
                    + bytes((0, 1, 0, 0, 1, difficulty, 1, adjustable, 2, 0, 1, 1, 13, area.rank_kind, 0, 0)),
                )
                self.assertEqual(
                    record[424:424 + 19],
                    b'\x34\x12\x04'
                    + bytes((0, 1, 0, 0, 1, difficulty, 0, 0, 2, 0, 0, 0, 13, area.rank_kind, 0, 0)),
                )
                self.assertFalse(any(record[364 + 19:424]))
                self.assertFalse(any(record[424 + 19:484]))
        self.assertEqual(self._record(0)[28:52].rstrip(b'\x00'), b'Easy 1')
        self.assertEqual(self._record(9)[28:52].rstrip(b'\x00'), b'Free Rules 2')

    def test_default_rule_values(self) -> None:
        # Record 0: EASY fixed, rank kind 0; record 8: host choice, rank kind 4.
        self.assertEqual(self._record(0)[364:383], bytes.fromhex('f7e104' '00010000' '01000100' '02000101' '0d000000'))
        self.assertEqual(self._record(8)[364:383], bytes.fromhex('f7e104' '00010000' '01010101' '02000101' '0d040000'))
        self.assertEqual(self._record(8)[424:443], bytes.fromhex('341204' '00010000' '01010000' '02000000' '0d040000'))

    def test_trailer_and_empty_custom_rules(self) -> None:
        self.assertEqual(self.table[4840:4844], b'\x00\x06\x1f\x00')
        self.assertFalse(any(self.table[4844:6012]))

    def test_token(self) -> None:
        token = data_token(self.directory)
        self.assertLessEqual(len(token), 31)
        self.assertEqual(token, data_token(build_directory()))
        areas = list(DEFAULT_AREAS)
        areas[3] = {**areas[3], 'title': 'Renamed'}
        self.assertNotEqual(data_token(build_directory(areas=tuple(areas))), token)


class Outbreak2DirectoryAreasTests(unittest.TestCase):
    """The ten Areas and the `OPENSNAP_OUTBREAK2_AREAS` setting."""

    def test_default_areas(self) -> None:
        areas = build_directory().areas
        self.assertEqual([area.number for area in areas], list(range(1, 11)))
        self.assertEqual([area.name for area in areas], [f'AREA{number:02d}' for number in range(1, 11)])
        self.assertEqual([area.fixed_difficulty for area in areas], [0, 0, 1, 1, 2, 2, 3, 3, None, None])
        self.assertEqual([area.rank_kind for area in areas], [0, 0, 1, 1, 2, 2, 3, 3, 4, 4])

    @staticmethod
    def _read(areas: object) -> object:
        setting = areas if isinstance(areas, str) else json.dumps(areas)
        environment = {
            'OPENSNAP_OUTBREAK2_AREAS': setting,
            'OPENSNAP_OUTBREAK2_SERVER_NAME': '',
            'OPENSNAP_OUTBREAK2_SERVER_DESCRIPTION': '',
            'OPENSNAP_OUTBREAK2_AREA_KEY': '',
            'OPENSNAP_OUTBREAK2_CAPACITY': '',
        }
        with mock.patch.dict(os.environ, environment):
            return read_directory()

    @staticmethod
    def _areas(**changes: object) -> list[dict[str, object]]:
        return [
            {'title': f'Room {number}', 'subtitle': 'SUB', 'description': 'Text.', 'difficulty': 'hard', 'rank_kind': 5}
            | changes
            for number in range(1, 11)
        ]

    def test_setting_applies_the_areas(self) -> None:
        areas = self._areas()
        areas[0] = {**areas[0], 'difficulty': 'Very Hard', 'rank_kind': 7}
        areas[1] = {**areas[1], 'difficulty': 'host', 'rank_kind': 0}
        directory = self._read(areas)
        self.assertEqual([area.title for area in directory.areas], [f'Room {number}' for number in range(1, 11)])
        self.assertEqual([area.fixed_difficulty for area in directory.areas], [3, None] + [2] * 8)
        self.assertEqual([area.rank_kind for area in directory.areas], [7, 0] + [5] * 8)

    def test_setting_rejects_invalid_areas(self) -> None:
        for areas in (
            self._areas()[:9],
            self._areas(title='x' * 24),
            self._areas(difficulty='nightmare'),
            self._areas(rank_kind=8),
            {'title': 'Not a list'},
        ):
            with self.subTest(areas=areas), self.assertRaises(ValueError):
                self._read(areas)
        with self.assertRaises(json.JSONDecodeError):
            self._read('[not json')


def _result(
    scenario: int, *, rank_kind: int, character: int, clear_ticks: int, points: int
) -> ScenarioResult:
    return ScenarioResult(
        scenario, rank_kind, clear_ticks != 0, character, clear_ticks, points, (), 32, 8, 0, 0, 286, 2027
    )


def _scenario_report(
    scenario: int,
    *,
    sequence: int,
    rank_kind: int = 0,
    character: int = 3,
    clear_ticks: int = 36000,
    points: int = 4000,
    teammates: tuple[tuple[bytes, int, int], ...] = (),
    network: tuple[int, ...] = (32, 0, 8, 0, 286, 2027),
    peak_messages: int = 0,
    flags: int = 0,
) -> bytes:
    """A 6301 payload as File#2 NA `0x001fd530` builds it; a zero clear time means no clear.

    The second byte is the room's rank kind (0..7). `teammates` are (name, status 1 alive /
    2 not, character); `network` the echo time, network error, echo replies and losses,
    peak queue and its location; the last two words the peak message count and flag word.
    """

    cleared = int(clear_ticks != 0)
    slots = [*teammates, *([(b'', 0, 0)] * (3 - len(teammates)))]
    return (
        bytes([cleared, rank_kind, scenario])
        + encode_app_field(b'jill0\x00', sequence)
        + bytes([cleared, character])
        + struct.pack('>LL', clear_ticks, points)
        + b''.join(encode_app_field(name + b'\x00', sequence) + bytes([status, code]) for name, status, code in slots)
        + struct.pack('>8L', *network, peak_messages, flags)
    )


if __name__ == '__main__':
    unittest.main()
