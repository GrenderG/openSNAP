"""Auto Modellista beta1 plugin regression tests."""

from dataclasses import replace
import struct
import tempfile
import unittest

from opensnap.config import StorageConfig, default_app_config
from opensnap.core.engine import SnapProtocolEngine
from opensnap.plugins.automodellista_beta1 import AutoModellistaBeta1Plugin
from opensnap.protocol import commands
from opensnap.protocol.constants import FLAG_CHANNEL_BITS, FLAG_MULTI, FLAG_RELIABLE, FLAG_RESPONSE, FLAG_ROOM
from opensnap.protocol.models import Endpoint, SnapMessage, WIRE_FORMAT_AM_BETA1_LEGACY

LEGACY_ROOM_ENTRY_COMMAND = 0x6406


class AutoModellistaBeta1PluginTests(unittest.TestCase):
    """Verify beta1-specific room-entry behavior."""

    def setUp(self) -> None:
        self._temp_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp_directory.cleanup)
        base_config = default_app_config()
        self._config = replace(
            base_config,
            server=replace(
                base_config.server,
                game_plugin='automodellista_beta1',
                game_identifier='automodellista_beta1',
            ),
            storage=StorageConfig(
                backend='sqlite',
                sqlite_path=f'{self._temp_directory.name}/automodellista-beta1.sqlite',
            ),
        )
        self._engine = SnapProtocolEngine(
            config=self._config,
            plugin=AutoModellistaBeta1Plugin(),
        )
        self.addCleanup(self._engine.close)

    def test_legacy_room_entry_query_returns_current_and_max_players(self) -> None:
        endpoint_one = Endpoint(host='127.0.0.1', port=51101)
        endpoint_two = Endpoint(host='127.0.0.2', port=51102)
        session_one = _create_session_via_login(self._engine, endpoint_one, 'test')
        session_two = _create_session_via_login(self._engine, endpoint_two, 'test')

        _join_lobby(self._engine, endpoint_one, session_one, lobby_id=1, sequence=1)
        _join_lobby(self._engine, endpoint_two, session_two, lobby_id=1, sequence=1)
        room_id = _create_room(
            self._engine,
            endpoint_one,
            session_one,
            sequence=2,
            room_name='beta1 room',
        )
        _join_room(
            self._engine,
            endpoint_two,
            session_two,
            room_id=room_id,
            sequence=2,
        )

        request = SnapMessage(
            endpoint=endpoint_two,
            type_flags=1,
            packet_number=0,
            command=LEGACY_ROOM_ENTRY_COMMAND,
            session_id=0,
            sequence_number=0,
            acknowledge_number=0,
            payload=struct.pack('>H', room_id) + _pack_legacy_string(''),
            wire_format=WIRE_FORMAT_AM_BETA1_LEGACY,
        )
        result = self._engine.handle_datagram(self._engine.encode_messages([request]), endpoint_two)

        self.assertFalse(result.errors)
        self.assertEqual(len(result.messages), 2)

        response_map = {message.command: message for message in result.messages}
        self.assertEqual(response_map[0x6403].wire_format, WIRE_FORMAT_AM_BETA1_LEGACY)
        self.assertEqual(struct.unpack('>2H', response_map[0x6403].payload), (room_id, 2))
        self.assertEqual(struct.unpack('>6H', response_map[0x640B].payload), (room_id, 4, 0, 0, 0, 0))

    def test_room_query_attribute_user_reply_keeps_shared_u32_count_field(self) -> None:
        endpoint_one = Endpoint(host='127.0.0.1', port=51111)
        endpoint_two = Endpoint(host='127.0.0.2', port=51112)
        session_one = _create_session_via_login(self._engine, endpoint_one, 'test')
        session_two = _create_session_via_login(self._engine, endpoint_two, 'test')

        _join_lobby(self._engine, endpoint_one, session_one, lobby_id=1, sequence=1)
        _join_lobby(self._engine, endpoint_two, session_two, lobby_id=1, sequence=1)
        room_id = _create_room(
            self._engine,
            endpoint_one,
            session_one,
            sequence=2,
            room_name='beta1 room',
        )
        _join_room(
            self._engine,
            endpoint_two,
            session_two,
            room_id=room_id,
            sequence=2,
        )

        request = SnapMessage(
            endpoint=endpoint_two,
            type_flags=FLAG_ROOM,
            packet_number=0,
            command=commands.CMD_QUERY_ATTRIBUTE,
            session_id=session_two,
            sequence_number=3,
            acknowledge_number=0,
            payload=struct.pack('>L4s', room_id, b'USER'),
        )
        result = self._engine.handle_datagram(self._engine.encode_messages([request]), endpoint_two)

        self.assertFalse(result.errors)
        self.assertEqual(len(result.messages), 1)
        self.assertEqual(result.messages[0].command, commands.CMD_QUERY_ATTRIBUTE)
        self.assertEqual(result.messages[0].payload, struct.pack('>L4sL', room_id, b'USER', 2))
        self.assertEqual(struct.unpack_from('>L', result.messages[0].payload, 8)[0], 2)

    def test_room_multi_query_attribute_does_not_expand_into_lobby_user_burst(self) -> None:
        endpoint_one = Endpoint(host='127.0.0.1', port=51113)
        endpoint_two = Endpoint(host='127.0.0.2', port=51114)
        session_one = _create_session_via_login(self._engine, endpoint_one, 'test')
        session_two = _create_session_via_login(self._engine, endpoint_two, 'test')

        _join_lobby(self._engine, endpoint_one, session_one, lobby_id=1, sequence=1)
        _join_lobby(self._engine, endpoint_two, session_two, lobby_id=1, sequence=1)
        room_id = _create_room(
            self._engine,
            endpoint_one,
            session_one,
            sequence=2,
            room_name='beta1 room',
        )

        query = SnapMessage(
            endpoint=endpoint_one,
            type_flags=FLAG_ROOM | FLAG_MULTI | FLAG_RELIABLE,
            packet_number=0,
            command=commands.CMD_QUERY_ATTRIBUTE,
            session_id=session_one,
            sequence_number=7,
            acknowledge_number=0,
            payload=struct.pack('>L4s', room_id, b'USER'),
            size_word_override=(FLAG_ROOM | FLAG_MULTI | FLAG_RELIABLE) | 0x0018,
        )
        result = self._engine.handle_datagram(self._engine.encode_messages([query]), endpoint_one)

        self.assertFalse(result.errors)
        self.assertEqual([message.command for message in result.messages], [commands.CMD_QUERY_ATTRIBUTE])
        self.assertEqual(result.messages[0].type_flags, FLAG_ROOM | FLAG_RESPONSE)
        self.assertEqual(result.messages[0].payload, struct.pack('>L4sL', room_id, b'USER', 1))
        self.assertEqual(struct.unpack_from('>L', result.messages[0].payload, 8)[0], 1)

    def test_multi_lobby_user_query_after_room_exit_uses_normal_single_reply(self) -> None:
        endpoint = Endpoint(host='127.0.0.1', port=51116)
        session_id = _create_session_via_login(self._engine, endpoint, 'test')
        _join_lobby(self._engine, endpoint, session_id, lobby_id=1, sequence=1)

        query = SnapMessage(
            endpoint=endpoint,
            type_flags=FLAG_CHANNEL_BITS | FLAG_MULTI | FLAG_RELIABLE,
            packet_number=0,
            command=commands.CMD_QUERY_ATTRIBUTE,
            session_id=session_id,
            sequence_number=12,
            acknowledge_number=0,
            payload=struct.pack('>L4s', 1, b'USER'),
            size_word_override=(FLAG_CHANNEL_BITS | FLAG_MULTI | FLAG_RELIABLE) | 0x0018,
        )
        status = SnapMessage(
            endpoint=endpoint,
            type_flags=FLAG_CHANNEL_BITS | FLAG_RELIABLE,
            packet_number=1,
            command=commands.CMD_CHANGE_USER_STATUS,
            session_id=session_id,
            sequence_number=0,
            acknowledge_number=0,
            payload=struct.pack('>L', 0xF7C00001),
        )

        result = self._engine.handle_datagram(self._engine.encode_messages([query, status]), endpoint)
        self.assertFalse(result.errors)
        self.assertEqual(len(result.messages), 2)

        query_reply = result.messages[0]
        self.assertEqual(query_reply.command, commands.CMD_QUERY_ATTRIBUTE)
        self.assertEqual(query_reply.type_flags, FLAG_CHANNEL_BITS | FLAG_RESPONSE)
        self.assertEqual(query_reply.acknowledge_number, 12)
        self.assertEqual(query_reply.payload, struct.pack('>L4sL', 1, b'USER', 1))

        status_reply = result.messages[1]
        self.assertEqual(status_reply.command, commands.CMD_RESULT_WRAPPER)
        self.assertEqual(status_reply.acknowledge_number, 12)
        self.assertEqual(struct.unpack('>2L', status_reply.payload), (commands.CMD_CHANGE_USER_STATUS, 0))

    def test_room_join_notifies_host_with_session_id_at_payload_plus_0x10(self) -> None:
        endpoint_one = Endpoint(host='127.0.0.1', port=51121)
        endpoint_two = Endpoint(host='127.0.0.2', port=51122)
        host_session = _create_session_via_login(self._engine, endpoint_one, 'test')
        joiner_session = _create_session_via_login(self._engine, endpoint_two, 'test')

        _join_lobby(self._engine, endpoint_one, host_session, lobby_id=1, sequence=1)
        _join_lobby(self._engine, endpoint_two, joiner_session, lobby_id=1, sequence=1)
        room_id = _create_room(
            self._engine,
            endpoint_one,
            host_session,
            sequence=2,
            room_name='beta1 room',
        )

        request = SnapMessage(
            endpoint=endpoint_two,
            type_flags=FLAG_ROOM,
            packet_number=0,
            command=commands.CMD_JOIN,
            session_id=joiner_session,
            sequence_number=2,
            acknowledge_number=0,
            payload=struct.pack('>L', room_id),
        )
        result = self._engine.handle_datagram(self._engine.encode_messages([request]), endpoint_two)

        self.assertFalse(result.errors)
        self.assertEqual(len(result.messages), 3)

        host_messages = [message for message in result.messages if message.endpoint == endpoint_one]
        self.assertEqual(len(host_messages), 2)

        host_join_callback = next(message for message in host_messages if message.command == commands.CMD_JOIN)
        self.assertEqual(host_join_callback.session_id, host_session)
        self.assertEqual(len(host_join_callback.payload), 40)
        self.assertEqual(struct.unpack_from('>L', host_join_callback.payload, 16)[0], joiner_session)
        self.assertEqual(host_join_callback.payload[0:16].rstrip(b'\x00').decode('utf-8'), 'test\n')

        host_user_callback = next(
            message for message in host_messages
            if message.command == commands.CMD_QUERY_ATTRIBUTE
        )
        self.assertEqual(host_user_callback.payload, struct.pack('>L4sL', room_id, b'USER', 2))

    def test_duplicate_reliable_room_join_returns_wrapper_only_after_first_host_callback(self) -> None:
        endpoint_one = Endpoint(host='127.0.0.1', port=51126)
        endpoint_two = Endpoint(host='127.0.0.2', port=51127)
        host_session = _create_session_via_login(self._engine, endpoint_one, 'test')
        joiner_session = _create_session_via_login(self._engine, endpoint_two, 'test')

        _join_lobby(self._engine, endpoint_one, host_session, lobby_id=1, sequence=1)
        _join_lobby(self._engine, endpoint_two, joiner_session, lobby_id=1, sequence=1)
        room_id = _create_room(
            self._engine,
            endpoint_one,
            host_session,
            sequence=2,
            room_name='beta1 room',
        )

        request = SnapMessage(
            endpoint=endpoint_two,
            type_flags=FLAG_ROOM | FLAG_RELIABLE,
            packet_number=0,
            command=commands.CMD_JOIN,
            session_id=joiner_session,
            sequence_number=18,
            acknowledge_number=0,
            payload=struct.pack('>L', room_id),
        )

        first_result = self._engine.handle_datagram(_encode(self._engine, request), endpoint_two)
        self.assertFalse(first_result.errors)
        self.assertEqual(len(first_result.messages), 3)
        first_host_callback = next(
            message
            for message in first_result.messages
            if message.endpoint == endpoint_one and message.command == commands.CMD_JOIN
        )
        first_host_user_callback = next(
            message
            for message in first_result.messages
            if message.endpoint == endpoint_one and message.command == commands.CMD_QUERY_ATTRIBUTE
        )
        first_wrapper = next(
            message for message in first_result.messages if message.endpoint == endpoint_two
        )

        duplicate_result = self._engine.handle_datagram(_encode(self._engine, request), endpoint_two)
        self.assertFalse(duplicate_result.errors)
        self.assertEqual(len(duplicate_result.messages), 1)
        duplicate_wrapper = next(
            message for message in duplicate_result.messages if message.endpoint == endpoint_two
        )
        self.assertGreater(duplicate_wrapper.sequence_number, first_wrapper.sequence_number)
        self.assertEqual(duplicate_wrapper.payload, first_wrapper.payload)
        self.assertEqual(first_host_callback.command, commands.CMD_JOIN)
        self.assertEqual(first_host_user_callback.payload, struct.pack('>L4sL', room_id, b'USER', 2))

    def test_room_leave_notifies_host_with_leaving_session_id(self) -> None:
        endpoint_one = Endpoint(host='127.0.0.1', port=51131)
        endpoint_two = Endpoint(host='127.0.0.2', port=51132)
        host_session = _create_session_via_login(self._engine, endpoint_one, 'test')
        leaver_session = _create_session_via_login(self._engine, endpoint_two, 'test')

        _join_lobby(self._engine, endpoint_one, host_session, lobby_id=1, sequence=1)
        _join_lobby(self._engine, endpoint_two, leaver_session, lobby_id=1, sequence=1)
        room_id = _create_room(
            self._engine,
            endpoint_one,
            host_session,
            sequence=2,
            room_name='beta1 room',
        )
        _join_room(
            self._engine,
            endpoint_two,
            leaver_session,
            room_id=room_id,
            sequence=2,
        )

        request = SnapMessage(
            endpoint=endpoint_two,
            type_flags=FLAG_ROOM,
            packet_number=0,
            command=commands.CMD_LEAVE,
            session_id=leaver_session,
            sequence_number=3,
            acknowledge_number=0,
            payload=b'',
        )
        result = self._engine.handle_datagram(self._engine.encode_messages([request]), endpoint_two)

        self.assertFalse(result.errors)
        self.assertEqual(len(result.messages), 3)

        host_messages = [message for message in result.messages if message.endpoint == endpoint_one]
        self.assertEqual(len(host_messages), 2)

        host_leave_callback = next(message for message in host_messages if message.command == commands.CMD_LEAVE)
        self.assertEqual(host_leave_callback.command, commands.CMD_LEAVE)
        self.assertEqual(host_leave_callback.session_id, host_session)
        self.assertEqual(struct.unpack('>L', host_leave_callback.payload)[0], leaver_session)

        host_user_callback = next(
            message for message in host_messages if message.command == commands.CMD_QUERY_ATTRIBUTE
        )
        self.assertEqual(host_user_callback.payload, struct.pack('>L4sL', room_id, b'USER', 1))


def _encode(engine: SnapProtocolEngine, message: SnapMessage) -> bytes:
    """Encode one SNAP message as one datagram."""

    return engine.encode_messages([message])


def _create_session_via_login(engine: SnapProtocolEngine, endpoint: Endpoint, username: str) -> int:
    """Create one session with the bootstrap login command."""

    request = SnapMessage(
        endpoint=endpoint,
        type_flags=FLAG_CHANNEL_BITS,
        packet_number=0,
        command=commands.CMD_LOGIN_CLIENT,
        session_id=0,
        sequence_number=0,
        acknowledge_number=0,
        payload=f'{username}\n\x00'.encode('utf-8'),
    )
    result = engine.handle_datagram(_encode(engine, request), endpoint)
    assert not result.errors
    assert result.messages
    return result.messages[0].session_id


def _join_lobby(
    engine: SnapProtocolEngine,
    endpoint: Endpoint,
    session_id: int,
    *,
    lobby_id: int,
    sequence: int,
) -> None:
    """Join one lobby for a session."""

    request = SnapMessage(
        endpoint=endpoint,
        type_flags=FLAG_CHANNEL_BITS,
        packet_number=0,
        command=commands.CMD_JOIN,
        session_id=session_id,
        sequence_number=sequence,
        acknowledge_number=0,
        payload=struct.pack('>L', lobby_id),
    )
    result = engine.handle_datagram(_encode(engine, request), endpoint)
    assert not result.errors


def _join_room(
    engine: SnapProtocolEngine,
    endpoint: Endpoint,
    session_id: int,
    *,
    room_id: int,
    sequence: int,
) -> None:
    """Join one room for a session."""

    request = SnapMessage(
        endpoint=endpoint,
        type_flags=FLAG_ROOM,
        packet_number=0,
        command=commands.CMD_JOIN,
        session_id=session_id,
        sequence_number=sequence,
        acknowledge_number=0,
        payload=struct.pack('>L', room_id),
    )
    result = engine.handle_datagram(_encode(engine, request), endpoint)
    assert not result.errors
    assert result.messages
    assert result.messages[0].command == commands.CMD_RESULT_WRAPPER


def _create_room(
    engine: SnapProtocolEngine,
    endpoint: Endpoint,
    session_id: int,
    *,
    sequence: int,
    room_name: str,
) -> int:
    """Create one room and return its id."""

    payload = bytearray(0x2C)
    payload[0:16] = room_name.encode('utf-8')[:16].ljust(16, b'\x00')
    struct.pack_into('>L', payload, 0x10, 4)
    payload[0x14:0x24] = b'pw'.ljust(16, b'\x00')
    struct.pack_into('>L', payload, 0x28, 1)
    request = SnapMessage(
        endpoint=endpoint,
        type_flags=0xB000,
        packet_number=0,
        command=commands.CMD_CREATE_GAME_ROOM,
        session_id=session_id,
        sequence_number=sequence,
        acknowledge_number=0,
        payload=bytes(payload),
    )
    result = engine.handle_datagram(_encode(engine, request), endpoint)
    assert not result.errors
    assert result.messages
    _, room_id = struct.unpack_from('>2L', result.messages[0].payload)
    return room_id


def _pack_legacy_string(value: str) -> bytes:
    """Pack one beta1 legacy length-prefixed string."""

    encoded = value.encode('utf-8')
    return struct.pack('>H', len(encoded)) + encoded


if __name__ == '__main__':
    unittest.main()
