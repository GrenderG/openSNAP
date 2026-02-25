"""Engine integration tests."""

from dataclasses import replace
import struct
import tempfile
import unittest

from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.decrepit.ciphers import algorithms as decrepit_algorithms
from cryptography.hazmat.primitives.ciphers import Cipher, modes

from opensnap.config import StorageConfig, default_app_config
from opensnap.core.engine import SnapProtocolEngine
from opensnap.plugins.automodellista import AutoModellistaPlugin
from opensnap.protocol import commands
from opensnap.protocol.constants import CHANNEL_LOBBY, CHANNEL_ROOM, FLAG_RELIABLE
from opensnap.protocol.fields import get_u32
from opensnap.protocol.models import Endpoint, SnapMessage


class EngineFlowTests(unittest.TestCase):
    """Smoke tests for main login and lobby flow."""

    def setUp(self) -> None:
        self._temp_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp_directory.cleanup)
        self._config = replace(
            default_app_config(),
            storage=StorageConfig(
                backend='sqlite',
                sqlite_path=f'{self._temp_directory.name}/engine-flow.sqlite',
            ),
        )

    def test_login_and_kics_flow(self) -> None:
        config = self._config
        engine = SnapProtocolEngine(config=config, plugin=AutoModellistaPlugin())
        endpoint = Endpoint(host='127.0.0.1', port=50000)

        login_request = SnapMessage(
            endpoint=endpoint,
            type_flags=CHANNEL_LOBBY,
            packet_number=0,
            command=commands.CMD_LOGIN_CLIENT,
            session_id=0,
            sequence_number=0,
            acknowledge_number=0,
            payload=b'test\n\x00',
        )
        login_result = engine.handle_datagram(_encode(login_request), endpoint)
        self.assertFalse(login_result.errors)
        self.assertEqual(len(login_result.messages), 1)
        self.assertEqual(login_result.messages[0].command, commands.CMD_BOOTSTRAP_LOGIN_SWAN)

        session_id = login_result.messages[0].session_id
        check_request = SnapMessage(
            endpoint=endpoint,
            type_flags=CHANNEL_LOBBY,
            packet_number=0,
            command=commands.CMD_BOOTSTRAP_LOGIN_SWAN_CHECK,
            session_id=session_id,
            sequence_number=1,
            acknowledge_number=0,
            payload=_build_valid_bootstrap_check_payload(config.server.bootstrap_key, config.server.server_secret),
        )
        check_result = engine.handle_datagram(_encode(check_request), endpoint)
        self.assertFalse(check_result.errors)
        self.assertEqual(len(check_result.messages), 1)
        self.assertEqual(check_result.messages[0].command, commands.CMD_BOOTSTRAP_LOGIN_SUCCESS)

        team_payload = bytearray(0x130)
        team_payload[0x128:0x12F] = b'team-a\x00'
        kics_request = SnapMessage(
            endpoint=endpoint,
            type_flags=CHANNEL_LOBBY,
            packet_number=0,
            command=commands.CMD_LOGIN_TO_KICS,
            session_id=session_id,
            sequence_number=2,
            acknowledge_number=0,
            payload=bytes(team_payload),
        )
        kics_result = engine.handle_datagram(_encode(kics_request), endpoint)
        self.assertFalse(kics_result.errors)
        self.assertEqual(len(kics_result.messages), 1)
        self.assertEqual(kics_result.messages[0].command, 0x29)
        self.assertEqual(get_u32(kics_result.messages[0].payload, 8), session_id)

    def test_lobby_query_returns_all_configured_lobbies(self) -> None:
        config = self._config
        engine = SnapProtocolEngine(config=config, plugin=AutoModellistaPlugin())
        endpoint = Endpoint(host='127.0.0.1', port=50001)

        request = SnapMessage(
            endpoint=endpoint,
            type_flags=CHANNEL_LOBBY,
            packet_number=0,
            command=commands.CMD_QUERY_LOBBIES,
            session_id=0,
            sequence_number=0,
            acknowledge_number=0,
            payload=b'',
        )
        result = engine.handle_datagram(_encode(request), endpoint)
        self.assertFalse(result.errors)
        self.assertEqual(len(result.messages), 1)

        payload = result.messages[0].payload
        lobby_count = struct.unpack_from('>L', payload, 8)[0]
        self.assertEqual(lobby_count, len(config.lobbies))

    def test_lobby_chat_broadcasts_to_lobby_members_and_acks_sender(self) -> None:
        config = self._config
        engine = SnapProtocolEngine(config=config, plugin=AutoModellistaPlugin())
        endpoint_one = Endpoint(host='127.0.0.1', port=50010)
        endpoint_two = Endpoint(host='127.0.0.2', port=50011)

        sender_session = _create_session_via_login(engine, endpoint_one, 'test')
        receiver_session = _create_session_via_login(engine, endpoint_two, 'test')

        _join_lobby(engine, endpoint_one, sender_session, lobby_id=1, sequence=3)
        _join_lobby(engine, endpoint_two, receiver_session, lobby_id=1, sequence=3)

        chat_request = SnapMessage(
            endpoint=endpoint_one,
            type_flags=0x1400,
            packet_number=0,
            command=commands.CMD_SEND,
            session_id=sender_session,
            sequence_number=4,
            acknowledge_number=0,
            payload=b'\x04\x04testteamhello',
        )
        chat_result = engine.handle_datagram(_encode(chat_request), endpoint_one)
        self.assertFalse(chat_result.errors)

        # One ACK to sender plus one chat callback per lobby member.
        self.assertEqual(len(chat_result.messages), 3)
        ack_messages = [message for message in chat_result.messages if message.command == commands.CMD_ACK]
        chat_messages = [message for message in chat_result.messages if message.command == commands.CMD_SEND]
        self.assertEqual(len(ack_messages), 1)
        self.assertEqual(len(chat_messages), 2)

        self.assertEqual(ack_messages[0].session_id, sender_session)
        self.assertEqual(ack_messages[0].endpoint, endpoint_one)

        chat_session_ids = {message.session_id for message in chat_messages}
        self.assertEqual(chat_session_ids, {sender_session, receiver_session})
        chat_endpoints = {message.endpoint for message in chat_messages}
        self.assertEqual(chat_endpoints, {endpoint_one, endpoint_two})

    def test_room_chat_broadcasts_to_room_members_and_acks_sender(self) -> None:
        config = self._config
        engine = SnapProtocolEngine(config=config, plugin=AutoModellistaPlugin())
        endpoint_one = Endpoint(host='127.0.0.1', port=50020)
        endpoint_two = Endpoint(host='127.0.0.2', port=50021)

        sender_session = _create_session_via_login(engine, endpoint_one, 'test')
        receiver_session = _create_session_via_login(engine, endpoint_two, 'test')
        _join_lobby(engine, endpoint_one, sender_session, lobby_id=1, sequence=3)
        _join_lobby(engine, endpoint_two, receiver_session, lobby_id=1, sequence=3)

        room_id = _create_room(engine, endpoint_one, sender_session, sequence=4, room_name='room-chat')
        _join_room(engine, endpoint_two, receiver_session, room_id=room_id, sequence=4)

        chat_request = SnapMessage(
            endpoint=endpoint_one,
            type_flags=0x2400,
            packet_number=0,
            command=commands.CMD_SEND,
            session_id=sender_session,
            sequence_number=5,
            acknowledge_number=0,
            payload=b'\x04\x04testteamhello-room',
        )
        chat_result = engine.handle_datagram(_encode(chat_request), endpoint_one)
        self.assertFalse(chat_result.errors)

        self.assertEqual(len(chat_result.messages), 3)
        ack_messages = [message for message in chat_result.messages if message.command == commands.CMD_ACK]
        chat_messages = [message for message in chat_result.messages if message.command == commands.CMD_SEND]
        self.assertEqual(len(ack_messages), 1)
        self.assertEqual(len(chat_messages), 2)
        self.assertEqual(ack_messages[0].endpoint, endpoint_one)

        chat_session_ids = {message.session_id for message in chat_messages}
        self.assertEqual(chat_session_ids, {sender_session, receiver_session})
        chat_endpoints = {message.endpoint for message in chat_messages}
        self.assertEqual(chat_endpoints, {endpoint_one, endpoint_two})

    def test_send_subcommand_8006_broadcasts_to_other_room_members(self) -> None:
        config = self._config
        engine = SnapProtocolEngine(config=config, plugin=AutoModellistaPlugin())
        endpoint_one = Endpoint(host='127.0.0.1', port=50030)
        endpoint_two = Endpoint(host='127.0.0.2', port=50031)

        sender_session = _create_session_via_login(engine, endpoint_one, 'test')
        receiver_session = _create_session_via_login(engine, endpoint_two, 'test')
        _join_lobby(engine, endpoint_one, sender_session, lobby_id=1, sequence=3)
        _join_lobby(engine, endpoint_two, receiver_session, lobby_id=1, sequence=3)

        room_id = _create_room(engine, endpoint_one, sender_session, sequence=4, room_name='room-game')
        _join_room(engine, endpoint_two, receiver_session, room_id=room_id, sequence=4)

        game_request = SnapMessage(
            endpoint=endpoint_one,
            type_flags=CHANNEL_ROOM | FLAG_RELIABLE,
            packet_number=0,
            command=commands.CMD_SEND,
            session_id=sender_session,
            sequence_number=5,
            acknowledge_number=0,
            payload=struct.pack('>H', 0x8006) + b'\x00' * 12,
        )
        game_result = engine.handle_datagram(_encode(game_request), endpoint_one)
        self.assertFalse(game_result.errors)
        self.assertEqual(len(game_result.messages), 2)

        ack = game_result.messages[0]
        relay = game_result.messages[1]
        self.assertEqual(ack.command, commands.CMD_ACK)
        self.assertEqual(ack.endpoint, endpoint_one)
        self.assertEqual(relay.command, commands.CMD_SEND)
        self.assertEqual(relay.endpoint, endpoint_two)
        self.assertEqual(relay.session_id, receiver_session)
        self.assertEqual(relay.payload, game_request.payload)

    def test_create_room_rejects_when_lobby_has_50_rooms(self) -> None:
        config = self._config
        engine = SnapProtocolEngine(config=config, plugin=AutoModellistaPlugin())
        endpoint = Endpoint(host='127.0.0.1', port=50040)

        session_id = _create_session_via_login(engine, endpoint, 'test')
        _join_lobby(engine, endpoint, session_id, lobby_id=1, sequence=3)

        for index in range(50):
            room_id = _create_room(engine, endpoint, session_id, sequence=4 + index, room_name=f'r{index}')
            self.assertGreater(room_id, 0)

        overflow_request = _build_create_room_request(
            endpoint=endpoint,
            session_id=session_id,
            sequence=54,
            room_name='overflow',
        )
        overflow_result = engine.handle_datagram(_encode(overflow_request), endpoint)
        self.assertFalse(overflow_result.errors)
        self.assertEqual(len(overflow_result.messages), 1)
        self.assertEqual(overflow_result.messages[0].command, commands.CMD_RESULT_WRAPPER)
        self.assertEqual(struct.unpack_from('>2L', overflow_result.messages[0].payload), (0x04, 1))

    def test_out_of_order_sequence_is_rejected_for_authenticated_session(self) -> None:
        config = self._config
        engine = SnapProtocolEngine(config=config, plugin=AutoModellistaPlugin())
        endpoint = Endpoint(host='127.0.0.1', port=50050)

        session_id = _create_session_via_login(engine, endpoint, 'test')
        request = SnapMessage(
            endpoint=endpoint,
            type_flags=CHANNEL_LOBBY,
            packet_number=0,
            command=commands.CMD_JOIN,
            session_id=session_id,
            sequence_number=10,
            acknowledge_number=0,
            payload=struct.pack('>L', 1),
        )
        first_result = engine.handle_datagram(_encode(request), endpoint)
        self.assertFalse(first_result.errors)
        self.assertEqual(len(first_result.messages), 1)

        duplicate = SnapMessage(
            endpoint=endpoint,
            type_flags=CHANNEL_LOBBY,
            packet_number=0,
            command=commands.CMD_JOIN,
            session_id=session_id,
            sequence_number=10,
            acknowledge_number=0,
            payload=struct.pack('>L', 1),
        )
        duplicate_result = engine.handle_datagram(_encode(duplicate), endpoint)
        self.assertFalse(duplicate_result.errors)
        self.assertEqual(duplicate_result.messages, [])

        older = SnapMessage(
            endpoint=endpoint,
            type_flags=CHANNEL_LOBBY,
            packet_number=0,
            command=commands.CMD_JOIN,
            session_id=session_id,
            sequence_number=9,
            acknowledge_number=0,
            payload=struct.pack('>L', 1),
        )
        older_result = engine.handle_datagram(_encode(older), endpoint)
        self.assertFalse(older_result.errors)
        self.assertEqual(older_result.messages, [])

    def test_router_contains_all_snapsi_handler_commands(self) -> None:
        config = self._config
        engine = SnapProtocolEngine(config=config, plugin=AutoModellistaPlugin())
        registered = set(engine._router._handlers.keys())  # noqa: SLF001

        expected_from_snapsi = {
            commands.CMD_LOGIN_CLIENT,
            commands.CMD_BOOTSTRAP_LOGIN_SWAN_CHECK,
            commands.CMD_LOGIN_TO_KICS,
            commands.CMD_SEND_ECHO,
            commands.CMD_LOGOUT_CLIENT,
            commands.CMD_QUERY_LOBBIES,
            commands.CMD_QUERY_ATTRIBUTE,
            commands.CMD_JOIN,
            commands.CMD_LEAVE,
            commands.CMD_SEND,
            commands.CMD_SEND_TARGET,
            commands.CMD_QUERY_GAME_ROOMS,
            commands.CMD_QUERY_USER,
            commands.CMD_CREATE_GAME_ROOM,
            commands.CMD_CHANGE_USER_STATUS,
            commands.CMD_CHANGE_USER_PROPERTY,
            commands.CMD_CHANGE_ATTRIBUTE,
        }
        self.assertTrue(expected_from_snapsi.issubset(registered))


def _encode(message: SnapMessage) -> bytes:
    """Encode request message as datagram."""

    from opensnap.protocol.codec import encode_messages

    return encode_messages([message])


def _create_session_via_login(engine: SnapProtocolEngine, endpoint: Endpoint, username: str) -> int:
    """Create session by issuing login-client command."""

    login_request = SnapMessage(
        endpoint=endpoint,
        type_flags=CHANNEL_LOBBY,
        packet_number=0,
        command=commands.CMD_LOGIN_CLIENT,
        session_id=0,
        sequence_number=0,
        acknowledge_number=0,
        payload=f'{username}\n\x00'.encode('utf-8'),
    )
    result = engine.handle_datagram(_encode(login_request), endpoint)
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
        type_flags=CHANNEL_LOBBY,
        packet_number=0,
        command=commands.CMD_JOIN,
        session_id=session_id,
        sequence_number=sequence,
        acknowledge_number=0,
        payload=struct.pack('>L', lobby_id),
    )
    result = engine.handle_datagram(_encode(request), endpoint)
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
        type_flags=CHANNEL_ROOM,
        packet_number=0,
        command=commands.CMD_JOIN,
        session_id=session_id,
        sequence_number=sequence,
        acknowledge_number=0,
        payload=struct.pack('>L', room_id),
    )
    result = engine.handle_datagram(_encode(request), endpoint)
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
    """Create one room and return room id."""

    request = _build_create_room_request(
        endpoint=endpoint,
        session_id=session_id,
        sequence=sequence,
        room_name=room_name,
    )
    result = engine.handle_datagram(_encode(request), endpoint)
    assert not result.errors
    assert result.messages
    payload = result.messages[0].payload
    subcommand, value = struct.unpack_from('>2L', payload)
    assert subcommand == 0x04
    assert value > 0
    return value


def _build_create_room_request(
    *,
    endpoint: Endpoint,
    session_id: int,
    sequence: int,
    room_name: str,
) -> SnapMessage:
    """Build create-room request payload."""

    payload = bytearray(0x2C)
    payload[0:16] = room_name.encode('utf-8')[:16].ljust(16, b'\x00')
    struct.pack_into('>L', payload, 0x10, 4)
    payload[0x14:0x24] = b'pw'.ljust(16, b'\x00')
    struct.pack_into('>L', payload, 0x28, 1)
    return SnapMessage(
        endpoint=endpoint,
        type_flags=0xB000,
        packet_number=0,
        command=commands.CMD_CREATE_GAME_ROOM,
        session_id=session_id,
        sequence_number=sequence,
        acknowledge_number=0,
        payload=bytes(payload),
    )


def _build_valid_bootstrap_check_payload(bootstrap_key: bytes, server_secret: str) -> bytes:
    """Build encrypted payload accepted by bootstrap check."""

    plaintext = bytearray(136)
    plaintext[8:8 + len(server_secret)] = server_secret.encode('utf-8')
    plaintext[8 + len(server_secret)] = 0
    cipher = Cipher(decrepit_algorithms.Blowfish(bootstrap_key), modes.ECB(), backend=default_backend())
    encryptor = cipher.encryptor()
    padded = _pad_block(bytes(plaintext), 8)
    return encryptor.update(padded) + encryptor.finalize()


def _pad_block(payload: bytes, block_size: int) -> bytes:
    """Pad payload with null bytes."""

    missing = (-len(payload)) % block_size
    if missing == 0:
        return payload
    return payload + (b'\x00' * missing)


if __name__ == '__main__':
    unittest.main()
