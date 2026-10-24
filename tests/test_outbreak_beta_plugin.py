"""Resident Evil Outbreak public beta (`SLUS_290.89`) lobby rules."""

from pathlib import Path
import socket
import struct
import tempfile
import unittest

from opensnap.plugins.outbreak_beta import OutbreakBetaPlugin
from opensnap.protocol import commands
from opensnap.protocol.codec import encode_messages
from opensnap.protocol.constants import FLAG_CHANNEL_BITS, FLAG_RESPONSE, FLAG_RELIABLE
from opensnap.storage.sql import SqlAccountStore, SqlRecordStore
from opensnap.storage.sqlite import SqliteConnection
from opensnap_app.capcom.outbreak.service import APP_BUILD, PUBLIC_BETA_VERSION, AppService, AppServiceConfig
from opensnap_app.capcom.protocol import (
    APP_DIRECTION_REPLY,
    app_frame,
    decode_app_field,
    encode_app_field,
    frame_payload,
    pop_app_frame,
)
from opensnap_app.capcom.server import CapcomAppServer
from tests.test_outbreak_plugin import (
    LOBBY_FLAGS,
    ROOM_FLAGS,
    STATUS_IN_SCENARIO,
    OutbreakEngineHarness,
    _join_payload,
    _name_search,
    _scenario_report,
)

STATUS_IN_LOBBY = 0xF7C00001
SCENARIO_AREA_ID = 1
FREE_AREA_ID = 2


class OutbreakBetaLobbyTests(OutbreakEngineHarness):
    plugin_class = OutbreakBetaPlugin
    lobby_status = STATUS_IN_LOBBY
    area_id = FREE_AREA_ID

    def _area_rows(self, low: bytes, high: bytes) -> list[tuple[bytes, int]]:
        search = _name_search(low, 4, (b'NAME', (2 << 5) | 6, high.ljust(16, b'\x00')))
        reply = self._send(self.players[0], commands.CMD_QUERY_AREA, search, LOBBY_FLAGS)[0]
        count = struct.unpack_from('>L', reply.payload, 8)[0]
        rows = [struct.unpack_from('>16s5L', reply.payload, 12 + index * 36) for index in range(count)]
        return [(row[0].rstrip(b'\x00'), row[5]) for row in rows]

    def test_each_mode_has_its_own_area(self) -> None:
        self.assertEqual(self._area_rows(b'obbs01', b'obbs01'), [(b'obbs01', SCENARIO_AREA_ID)])
        self.assertEqual(self._area_rows(b'obbf01', b'obbf01'), [(b'obbf01', FREE_AREA_ID)])
        self.assertEqual(self._area_rows(b'obmf01', b'obmf99'), [])

    def test_free_mode_joins_lobby_0_outside_the_areas(self) -> None:
        player = self.players[0]
        joined = self._send(player, commands.CMD_JOIN, struct.pack('>L', 0), LOBBY_FLAGS)
        self.assertEqual(joined[0].type_flags, FLAG_CHANNEL_BITS | FLAG_RESPONSE | FLAG_RELIABLE)
        self.assertEqual(struct.unpack('>2L', joined[0].payload), (commands.CMD_JOIN, 0))
        self.assertEqual(self.engine._sessions.get(player[1]).lobby_id, 0)  # noqa: SLF001

    def test_room_list_is_one_reply(self) -> None:
        for index in range(30):
            self.engine._rooms.create_room(  # noqa: SLF001
                name=f'room{index}', password='', rules=0, max_players=4, lobby_id=FREE_AREA_ID,
                host_session_id=0x1000 + index,
            )

        pages = self._send(self.players[0], commands.CMD_QUERY_GAME_ROOMS, struct.pack('>L', FREE_AREA_ID), LOBBY_FLAGS)

        self.assertEqual(len(pages), 1)
        self.assertEqual(struct.unpack_from('>L', pages[0].payload, 8)[0], 24)
        self.assertLessEqual(len(encode_messages(pages)), 896)

    def test_reliable_echo_in_a_scenario_is_only_acked(self) -> None:
        # The beta's echo is reliable (`0x005835c0`): ACKed, never answered.
        host = self.players[0]
        self._send(host, commands.CMD_CHANGE_USER_STATUS, struct.pack('>L', STATUS_IN_SCENARIO))
        replies = self._send(host, commands.CMD_SEND_ECHO, b'\x01', ROOM_FLAGS)
        self.assertEqual([(m.command, m.sequence_number) for m in replies], [(commands.CMD_ACK, 0)])

    def test_scenario_window_ends_at_the_beta_lobby_status(self) -> None:
        host, guest, _ = self.players
        room_id = self._create_room(host)
        self._send(guest, commands.CMD_JOIN, _join_payload(room_id))
        for player in (host, guest):
            self._send(player, commands.CMD_CHANGE_USER_STATUS, struct.pack('>L', STATUS_IN_SCENARIO))
        target = struct.pack('>2L', 1, host[1]) + bytes((0, 0, 1, 0))
        self.assertFalse(self._to(self._send(guest, commands.CMD_SEND_TARGET, target), host, commands.CMD_SEND_TARGET))

        # The guest returns first; the host then leaves its room and Area before its lobby status.
        self._send(guest, commands.CMD_LEAVE)
        self._send(host, commands.CMD_LEAVE)
        self._send(host, commands.CMD_LEAVE, type_flags=LOBBY_FLAGS)
        back = self._send(host, commands.CMD_CHANGE_USER_STATUS, struct.pack('>L', STATUS_IN_LOBBY))
        # The guest's leave was about the room the host has left.
        self.assertFalse(self._to(back, host, commands.CMD_LEAVE))

        self._send(host, commands.CMD_JOIN, struct.pack('>L', FREE_AREA_ID), LOBBY_FLAGS)
        room_id = self._create_room(host)
        self._send(guest, commands.CMD_JOIN, _join_payload(room_id))
        self.assertTrue(self._to(self._send(guest, commands.CMD_SEND_TARGET, target), host, commands.CMD_SEND_TARGET))


class OutbreakBetaAppServiceTests(unittest.TestCase):
    """The beta's APP connections reach its own service by build stamp."""

    def setUp(self) -> None:
        temp_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temp_directory.cleanup)
        root = Path(temp_directory.name)
        connection = SqliteConnection(root / 'shared.sqlite')
        self.addCleanup(connection.close)
        self.accounts = SqlAccountStore(connection, server_secret='test secret')
        self.records = SqlRecordStore(connection)
        release = AppService(AppServiceConfig(data_directory=root / 'outbreak'), self.accounts, self.records)
        beta = AppService(
            AppServiceConfig(data_directory=root / 'outbreak_beta'), self.accounts, self.records, public_beta=True
        )
        (root / 'outbreak_beta' / 'files' / '01' / 'INFOR').mkdir(parents=True)
        (root / 'outbreak_beta' / 'files' / '01' / 'INFOR' / 'INFOR00.HTM').write_bytes(b'<BODY>beta<END>')
        server = CapcomAppServer(
            host='127.0.0.1', port=0, titles={APP_BUILD: release}, stamped_builds={PUBLIC_BETA_VERSION: beta}
        )
        server.start()
        self.addCleanup(server.stop)
        self.client = socket.create_connection(server.address, timeout=5)
        self.addCleanup(self.client.close)
        self.buffer = bytearray()

    def _receive(self) -> bytes:
        while (frame := pop_app_frame(self.buffer)) is None:
            self.buffer.extend(self.client.recv(65535))
        return frame

    def _request(self, command: int, sequence: int, payload: bytes = b'') -> bytes:
        self.client.sendall(app_frame(1, command, sequence=sequence, payload=payload))
        return frame_payload(self._receive())

    def _open(self) -> None:
        self._receive()
        answer = encode_app_field(b'jill0', 0) + b'\x01\x01' + encode_app_field(PUBLIC_BETA_VERSION, 0) + bytes(4)
        self.client.sendall(app_frame(APP_DIRECTION_REPLY, 0x1002, payload=answer))
        self._receive()

    def test_lobby_entry_gets_the_beta_page(self) -> None:
        self._open()
        name = b'01/INFOR/INFOR00.HTM'
        info = self._request(0x6101, 1, bytes(4) + encode_app_field(name, 1))
        _, size = decode_app_field(info[5:], 1)
        self.assertEqual(struct.unpack_from('>L', info, 5 + size)[0], len(b'<BODY>beta<END>') + 1)

    def test_results_go_on_the_beta_boards_only(self) -> None:
        account = self.accounts.create_user('jill0', 'secret')
        self._open()
        self._request(0x6301, 1, _scenario_report(1, sequence=1, network=(5, 0, 0, 0, 0, 0)))
        self.assertEqual(self.records.boards('outbreak'), [])
        self.assertEqual(self.records.boards('outbreak_beta'), ['clear-1-1', 'points'])
        [points] = self.records.top('outbreak_beta', 'points', 10)
        self.assertEqual((points.user_id, points.score), (account.user_id, 4000))
