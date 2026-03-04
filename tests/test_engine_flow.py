"""Engine integration tests."""

from dataclasses import replace
import struct
import tempfile
import unittest
from unittest.mock import patch

from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.decrepit.ciphers import algorithms as decrepit_algorithms
from cryptography.hazmat.primitives.ciphers import Cipher, modes

from opensnap.config import StorageConfig, default_app_config
from opensnap.core.engine import SnapProtocolEngine
from opensnap.plugins.automodellista import AutoModellistaPlugin
from opensnap.protocol import commands
from opensnap.protocol.constants import (
    CHANNEL_LOBBY,
    CHANNEL_ROOM,
    FLAG_LOBBY,
    FLAG_MULTI,
    FLAG_RELAY,
    FLAG_RELIABLE,
    FLAG_RESPONSE,
    FOOTER_BYTES_KAGE,
)
from opensnap.protocol.fields import get_c_string, get_u32
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
        success_clear = _decrypt_blowfish(config.server.bootstrap_key, check_result.messages[0].payload)
        self.assertEqual(get_c_string(success_clear, 0), 'test\n')
        self.assertNotEqual(get_u32(success_clear, 40), 0)
        self.assertEqual(get_u32(success_clear, 44), config.server.game.port)
        self.assertEqual(get_u32(success_clear, 48), config.server.game.port)
        self.assertEqual(get_u32(success_clear, 52), 0)
        self.assertEqual(get_u32(success_clear, 56), 0)
        self.assertEqual(get_u32(success_clear, 60), 0)

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

    def test_login_and_kics_flow_tolerates_zero_sequence_reuse(self) -> None:
        """Keep the observed bootstrap/login sequence-0 reuse behavior."""

        config = self._config
        engine = SnapProtocolEngine(config=config, plugin=AutoModellistaPlugin())
        endpoint = Endpoint(host='127.0.0.1', port=50002)

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
            sequence_number=0,
            acknowledge_number=0,
            payload=_build_valid_bootstrap_check_payload(config.server.bootstrap_key, config.server.server_secret),
        )
        check_result = engine.handle_datagram(_encode(check_request), endpoint)
        self.assertFalse(check_result.errors)
        self.assertEqual(len(check_result.messages), 1)
        self.assertEqual(check_result.messages[0].command, commands.CMD_BOOTSTRAP_LOGIN_SUCCESS)
        success_clear = _decrypt_blowfish(config.server.bootstrap_key, check_result.messages[0].payload)
        self.assertEqual(get_c_string(success_clear, 0), 'test\n')

        team_payload = bytearray(0x130)
        team_payload[0x128:0x12F] = b'team-a\x00'
        kics_request = SnapMessage(
            endpoint=endpoint,
            type_flags=CHANNEL_LOBBY,
            packet_number=0,
            command=commands.CMD_LOGIN_TO_KICS,
            session_id=session_id,
            sequence_number=0,
            acknowledge_number=0,
            payload=bytes(team_payload),
        )
        kics_result = engine.handle_datagram(_encode(kics_request), endpoint)
        self.assertFalse(kics_result.errors)
        self.assertEqual(len(kics_result.messages), 1)
        self.assertEqual(kics_result.messages[0].command, commands.CMD_RESULT_LOGIN_TO_KICS)
        self.assertEqual(get_u32(kics_result.messages[0].payload, 8), session_id)

    def test_login_client_accepts_repeated_login_payload(self) -> None:
        config = self._config
        engine = SnapProtocolEngine(config=config, plugin=AutoModellistaPlugin())
        endpoint = Endpoint(host='127.0.0.1', port=50004)

        duplicated_login = b'test\ntest\n'
        login_request = SnapMessage(
            endpoint=endpoint,
            type_flags=CHANNEL_LOBBY,
            packet_number=0,
            command=commands.CMD_LOGIN_CLIENT,
            session_id=0,
            sequence_number=0,
            acknowledge_number=0,
            payload=duplicated_login + (b'\x00' * (120 - len(duplicated_login))),
        )
        login_result = engine.handle_datagram(_encode(login_request), endpoint)

        self.assertFalse(login_result.errors)
        self.assertEqual(len(login_result.messages), 1)
        self.assertEqual(login_result.messages[0].command, commands.CMD_BOOTSTRAP_LOGIN_SWAN)

    def test_login_client_with_kage_footer_uses_kage_bootstrap_variant(self) -> None:
        config = self._config
        engine = SnapProtocolEngine(config=config, plugin=AutoModellistaPlugin())
        endpoint = Endpoint(host='127.0.0.1', port=50014)

        raw_login = b'test'
        login_request = SnapMessage(
            endpoint=endpoint,
            type_flags=CHANNEL_LOBBY,
            packet_number=0,
            command=commands.CMD_LOGIN_CLIENT,
            session_id=0,
            sequence_number=0,
            acknowledge_number=0,
            payload=raw_login + b'\x00',
        )

        with patch(
            'opensnap.core.bootstrap.handlers._resolve_local_host_for_client',
            return_value='127.0.0.1',
        ):
            from opensnap.protocol.codec import encode_messages

            login_result = engine.handle_datagram(
                encode_messages([login_request], footer_bytes=FOOTER_BYTES_KAGE),
                endpoint,
            )

        self.assertFalse(login_result.errors)
        self.assertEqual(len(login_result.messages), 1)
        challenge = login_result.messages[0]
        self.assertEqual(challenge.command, commands.CMD_BOOTSTRAP_LOGIN_SWAN)
        self.assertEqual(len(challenge.payload), 0x118)

        account = engine._accounts.get_by_name('test')
        assert account is not None
        clear = _decrypt_blowfish(account.bootstrap_magic_key.hex().encode('ascii'), challenge.payload)
        self.assertEqual(get_c_string(clear, 0), raw_login.decode('utf-8'))
        self.assertNotEqual(get_u32(clear, 40), 0)
        self.assertEqual(get_u32(clear, 44), config.server.bootstrap.port)
        self.assertEqual(get_u32(clear, 48), config.server.bootstrap.port)
        self.assertEqual(get_u32(clear, 52), 0)
        self.assertEqual(get_u32(clear, 56), 0)
        self.assertEqual(get_u32(clear, 60), len(clear) - struct.calcsize('>40s6L'))

    def test_login_client_with_primary_footer_and_single_string_uses_primary_bootstrap_variant(self) -> None:
        config = self._config
        engine = SnapProtocolEngine(config=config, plugin=AutoModellistaPlugin())
        endpoint = Endpoint(host='127.0.0.1', port=50015)

        login_request = SnapMessage(
            endpoint=endpoint,
            type_flags=CHANNEL_LOBBY,
            packet_number=0,
            command=commands.CMD_LOGIN_CLIENT,
            session_id=0,
            sequence_number=0,
            acknowledge_number=0,
            payload=b'test\x00',
        )

        login_result = engine.handle_datagram(_encode(login_request), endpoint)

        self.assertFalse(login_result.errors)
        self.assertEqual(len(login_result.messages), 1)
        challenge = login_result.messages[0]
        self.assertEqual(challenge.command, commands.CMD_BOOTSTRAP_LOGIN_SWAN)

        clear = _decrypt_blowfish(config.server.bootstrap_key, challenge.payload)
        account = engine._accounts.get_by_name('test')
        assert account is not None
        zero, port, seed_length = struct.unpack_from('>HHL', clear, 0)
        self.assertEqual(zero, 0)
        self.assertEqual(port, config.server.bootstrap.port)
        self.assertEqual(seed_length, len(account.seed.encode('utf-8')))

    def test_send_echo_returns_same_command_and_echoed_u32(self) -> None:
        engine = SnapProtocolEngine(config=self._config, plugin=AutoModellistaPlugin())
        endpoint = Endpoint(host='127.0.0.1', port=50016)
        request = SnapMessage(
            endpoint=endpoint,
            type_flags=CHANNEL_LOBBY,
            packet_number=0,
            command=commands.CMD_SEND_ECHO,
            session_id=0x12345678,
            sequence_number=9,
            acknowledge_number=0,
            payload=struct.pack('>I', 0xDEADBEEF) + b'extra',
        )

        result = engine.handle_datagram(_encode(request), endpoint)

        self.assertFalse(result.errors)
        self.assertEqual(len(result.messages), 1)
        response = result.messages[0]
        self.assertEqual(response.command, commands.CMD_SEND_ECHO)
        self.assertEqual(response.type_flags, CHANNEL_LOBBY | FLAG_RESPONSE)
        self.assertEqual(response.acknowledge_number, request.sequence_number)
        self.assertEqual(response.payload, struct.pack('>I', 0xDEADBEEF))

    def test_login_client_unknown_account_logs_warning(self) -> None:
        engine = SnapProtocolEngine(config=self._config, plugin=AutoModellistaPlugin())
        endpoint = Endpoint(host='127.0.0.1', port=50006)
        login_request = SnapMessage(
            endpoint=endpoint,
            type_flags=CHANNEL_LOBBY,
            packet_number=0,
            command=commands.CMD_LOGIN_CLIENT,
            session_id=0,
            sequence_number=0,
            acknowledge_number=0,
            payload=b'no-such-user\n\x00',
        )

        with self.assertLogs('opensnap.core.bootstrap', level='WARNING') as captured:
            login_result = engine.handle_datagram(_encode(login_request), endpoint)

        self.assertFalse(login_result.errors)
        self.assertEqual(len(login_result.messages), 1)
        self.assertEqual(login_result.messages[0].command, commands.CMD_BOOTSTRAP_LOGIN_FAIL)
        self.assertIn('account', '\n'.join(captured.output))

    def test_unhandled_command_reports_diagnostic_error(self) -> None:
        engine = SnapProtocolEngine(config=self._config, plugin=AutoModellistaPlugin())
        endpoint = Endpoint(host='127.0.0.1', port=50005)
        request = SnapMessage(
            endpoint=endpoint,
            type_flags=CHANNEL_LOBBY,
            packet_number=0,
            command=0x99,
            session_id=0x12345678,
            sequence_number=7,
            acknowledge_number=3,
            payload=b'\x12\x34',
        )

        with self.assertLogs('opensnap.engine', level='WARNING') as captured:
            result = engine.handle_datagram(_encode(request), endpoint)

        self.assertEqual(result.messages, [])
        self.assertEqual(len(result.errors), 1)
        self.assertIn('Unhandled command 0x99', result.errors[0])
        self.assertIn('payload_len=2', result.errors[0])
        self.assertIn('payload_hex=12 34', result.errors[0])
        self.assertIn('Unhandled command 0x99', '\n'.join(captured.output))

    def test_join_without_session_logs_warning(self) -> None:
        engine = SnapProtocolEngine(config=self._config, plugin=AutoModellistaPlugin())
        endpoint = Endpoint(host='127.0.0.1', port=50007)
        request = SnapMessage(
            endpoint=endpoint,
            type_flags=CHANNEL_LOBBY,
            packet_number=0,
            command=commands.CMD_JOIN,
            session_id=0xDEADBEEF,
            sequence_number=1,
            acknowledge_number=0,
            payload=struct.pack('>L', 1),
        )

        with self.assertLogs('opensnap.plugins.automodellista', level='WARNING') as captured:
            result = engine.handle_datagram(_encode(request), endpoint)

        self.assertEqual(result.messages, [])
        self.assertFalse(result.errors)
        self.assertIn('no session matched', '\n'.join(captured.output))

    def test_send_target_short_payload_logs_warning_and_acks(self) -> None:
        engine = SnapProtocolEngine(config=self._config, plugin=AutoModellistaPlugin())
        endpoint = Endpoint(host='127.0.0.1', port=50008)
        session_id = _create_session_via_login(engine, endpoint, 'test')

        request = SnapMessage(
            endpoint=endpoint,
            type_flags=CHANNEL_ROOM | FLAG_RELIABLE,
            packet_number=0,
            command=commands.CMD_SEND_TARGET,
            session_id=session_id,
            sequence_number=4,
            acknowledge_number=0,
            payload=b'\x81\x03',
        )

        with self.assertLogs('opensnap.plugins.automodellista', level='WARNING') as captured:
            result = engine.handle_datagram(_encode(request), endpoint)

        self.assertFalse(result.errors)
        self.assertEqual(len(result.messages), 1)
        self.assertEqual(result.messages[0].command, commands.CMD_ACK)
        self.assertIn('short send-target payload', '\n'.join(captured.output))

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

    def test_multi_query_attribute_burst_is_handled_once(self) -> None:
        config = self._config
        engine = SnapProtocolEngine(config=config, plugin=AutoModellistaPlugin())
        endpoint = Endpoint(host='127.0.0.1', port=50003)
        session_id = _create_session_via_login(engine, endpoint, 'test')

        first = SnapMessage(
            endpoint=endpoint,
            type_flags=CHANNEL_LOBBY | FLAG_MULTI,
            packet_number=0,
            command=commands.CMD_QUERY_ATTRIBUTE,
            session_id=session_id,
            sequence_number=2,
            acknowledge_number=0,
            payload=struct.pack('>L4s', 1, b'USER'),
            size_word_override=(CHANNEL_LOBBY | FLAG_MULTI) | 0x0018,
        )
        second = SnapMessage(
            endpoint=endpoint,
            type_flags=CHANNEL_LOBBY,
            packet_number=1,
            command=commands.CMD_QUERY_ATTRIBUTE,
            session_id=session_id,
            sequence_number=0,
            acknowledge_number=0,
            payload=struct.pack('>L4s', 2, b'USER'),
        )
        burst = _encode_many([first, second])

        result = engine.handle_datagram(burst, endpoint)
        self.assertFalse(result.errors)
        self.assertEqual(len(result.messages), 1)
        self.assertEqual(result.messages[0].command, commands.CMD_QUERY_ATTRIBUTE)

        payload = result.messages[0].payload
        self.assertEqual(struct.unpack_from('>L', payload, 8)[0], 0)
        self.assertEqual(struct.unpack_from('>H', payload, 12)[0], 0x501C)
        self.assertEqual(payload[14], 1)
        self.assertEqual(payload[15], commands.CMD_QUERY_ATTRIBUTE)
        self.assertEqual(struct.unpack_from('>L', payload, 28)[0], 2)
        self.assertEqual(
            len(payload),
            12 + ((len(config.lobbies) - 1) * 28),
        )

    def test_lobby_chat_broadcasts_to_other_lobby_members_and_acks_sender(self) -> None:
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
            type_flags=FLAG_RELAY | FLAG_LOBBY,
            packet_number=0,
            command=commands.CMD_SEND,
            session_id=sender_session,
            sequence_number=4,
            acknowledge_number=0,
            payload=b'\x04\x04testteamhello',
        )
        chat_result = engine.handle_datagram(_encode(chat_request), endpoint_one)
        self.assertFalse(chat_result.errors)

        # One ACK to sender plus one chat callback per peer lobby member.
        self.assertEqual(len(chat_result.messages), 2)
        ack_messages = [message for message in chat_result.messages if message.command == commands.CMD_ACK]
        chat_messages = [message for message in chat_result.messages if message.command == commands.CMD_SEND]
        self.assertEqual(len(ack_messages), 1)
        self.assertEqual(len(chat_messages), 1)

        self.assertEqual(ack_messages[0].session_id, sender_session)
        self.assertEqual(ack_messages[0].endpoint, endpoint_one)

        chat_session_ids = {message.session_id for message in chat_messages}
        self.assertEqual(chat_session_ids, {receiver_session})
        chat_endpoints = {message.endpoint for message in chat_messages}
        self.assertEqual(chat_endpoints, {endpoint_two})
        self.assertEqual(chat_messages[0].payload, chat_request.payload)

    def test_room_chat_broadcasts_to_other_room_members_and_acks_sender(self) -> None:
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
            type_flags=FLAG_RELAY | CHANNEL_ROOM,
            packet_number=0,
            command=commands.CMD_SEND,
            session_id=sender_session,
            sequence_number=5,
            acknowledge_number=0,
            payload=b'\x04\x04testteamhello-room',
        )
        chat_result = engine.handle_datagram(_encode(chat_request), endpoint_one)
        self.assertFalse(chat_result.errors)

        self.assertEqual(len(chat_result.messages), 2)
        ack_messages = [message for message in chat_result.messages if message.command == commands.CMD_ACK]
        chat_messages = [message for message in chat_result.messages if message.command == commands.CMD_SEND]
        self.assertEqual(len(ack_messages), 1)
        self.assertEqual(len(chat_messages), 1)
        self.assertEqual(ack_messages[0].endpoint, endpoint_one)

        chat_session_ids = {message.session_id for message in chat_messages}
        self.assertEqual(chat_session_ids, {receiver_session})
        chat_endpoints = {message.endpoint for message in chat_messages}
        self.assertEqual(chat_endpoints, {endpoint_two})
        self.assertEqual(chat_messages[0].payload, chat_request.payload)

    def test_room_join_notifies_existing_members(self) -> None:
        config = self._config
        engine = SnapProtocolEngine(config=config, plugin=AutoModellistaPlugin())
        endpoint_one = Endpoint(host='127.0.0.1', port=50022)
        endpoint_two = Endpoint(host='127.0.0.2', port=50023)

        host_session = _create_session_via_login(engine, endpoint_one, 'test')
        joiner_session = _create_session_via_login(engine, endpoint_two, 'test')
        _join_lobby(engine, endpoint_one, host_session, lobby_id=1, sequence=3)
        _join_lobby(engine, endpoint_two, joiner_session, lobby_id=1, sequence=3)

        room_id = _create_room(engine, endpoint_one, host_session, sequence=4, room_name='join-callback')
        join_request = SnapMessage(
            endpoint=endpoint_two,
            type_flags=CHANNEL_ROOM,
            packet_number=0,
            command=commands.CMD_JOIN,
            session_id=joiner_session,
            sequence_number=4,
            acknowledge_number=0,
            payload=struct.pack('>L', room_id),
        )
        join_result = engine.handle_datagram(_encode(join_request), endpoint_two)
        self.assertFalse(join_result.errors)
        self.assertEqual(len(join_result.messages), 2)

        join_ack = join_result.messages[0]
        self.assertEqual(join_ack.command, commands.CMD_RESULT_WRAPPER)
        self.assertEqual(join_ack.endpoint, endpoint_two)
        self.assertEqual(struct.unpack_from('>2L', join_ack.payload), (0x06, 0))

        callbacks = [message for message in join_result.messages[1:] if message.command == commands.CMD_JOIN]
        self.assertEqual(len(callbacks), 1)
        for callback in callbacks:
            self.assertEqual(callback.endpoint, endpoint_one)
            self.assertEqual(callback.session_id, host_session)
            self.assertEqual(callback.type_flags & FLAG_RESPONSE, FLAG_RESPONSE)
            self.assertEqual(callback.type_flags & FLAG_RELIABLE, 0)
            self.assertEqual(callback.acknowledge_number, 4)
            callback_username, callback_session_id, callback_unknown, callback_team = struct.unpack(
                '>16s2L16s',
                callback.payload,
            )
            self.assertEqual(callback_session_id, joiner_session)
            self.assertEqual(callback_unknown, 0)
            self.assertEqual(callback_username.rstrip(b'\x00').decode('utf-8'), 'test\n')
            self.assertEqual(callback_team.rstrip(b'\x00').decode('utf-8'), '')

    def test_lobby_join_result_wrapper_uses_operation_id(self) -> None:
        config = self._config
        engine = SnapProtocolEngine(config=config, plugin=AutoModellistaPlugin())
        endpoint = Endpoint(host='127.0.0.1', port=50060)

        session_id = _create_session_via_login(engine, endpoint, 'test')
        request = SnapMessage(
            endpoint=endpoint,
            type_flags=CHANNEL_LOBBY | FLAG_RELIABLE,
            packet_number=0,
            command=commands.CMD_JOIN,
            session_id=session_id,
            sequence_number=4,
            acknowledge_number=0,
            payload=struct.pack('>L', 1),
        )

        result = engine.handle_datagram(_encode(request), endpoint)
        self.assertFalse(result.errors)
        self.assertEqual(len(result.messages), 1)
        self.assertEqual(result.messages[0].command, commands.CMD_RESULT_WRAPPER)
        self.assertEqual(struct.unpack_from('>2L', result.messages[0].payload), (0x06, 0))

    def test_duplicate_room_join_retransmit_is_reply_only(self) -> None:
        config = self._config
        engine = SnapProtocolEngine(config=config, plugin=AutoModellistaPlugin())
        endpoint_one = Endpoint(host='127.0.0.1', port=50110)
        endpoint_two = Endpoint(host='127.0.0.2', port=50111)

        host_session = _create_session_via_login(engine, endpoint_one, 'test')
        joiner_session = _create_session_via_login(engine, endpoint_two, 'test')
        _join_lobby(engine, endpoint_one, host_session, lobby_id=1, sequence=3)
        _join_lobby(engine, endpoint_two, joiner_session, lobby_id=1, sequence=3)

        room_id = _create_room(engine, endpoint_one, host_session, sequence=4, room_name='dup-join-room')
        join_request = SnapMessage(
            endpoint=endpoint_two,
            type_flags=CHANNEL_ROOM | FLAG_RELIABLE,
            packet_number=0,
            command=commands.CMD_JOIN,
            session_id=joiner_session,
            sequence_number=18,
            acknowledge_number=0,
            payload=struct.pack('>L', room_id),
        )

        first_result = engine.handle_datagram(_encode(join_request), endpoint_two)
        self.assertFalse(first_result.errors)
        self.assertEqual(len(first_result.messages), 2)

        duplicate_result = engine.handle_datagram(_encode(join_request), endpoint_two)
        self.assertFalse(duplicate_result.errors)
        self.assertEqual(len(duplicate_result.messages), 1)
        self.assertEqual(duplicate_result.messages[0].command, commands.CMD_RESULT_WRAPPER)
        self.assertEqual(duplicate_result.messages[0].endpoint, endpoint_two)
        self.assertEqual(struct.unpack_from('>2L', duplicate_result.messages[0].payload), (0x06, 0))

    def test_room_join_callback_retries_on_tick_when_join_sync_stalls(self) -> None:
        config = self._config
        engine = SnapProtocolEngine(config=config, plugin=AutoModellistaPlugin())
        endpoint_one = Endpoint(host='127.0.0.1', port=50120)
        endpoint_two = Endpoint(host='127.0.0.2', port=50121)

        host_session = _create_session_via_login(engine, endpoint_one, 'test')
        joiner_session = _create_session_via_login(engine, endpoint_two, 'test')
        _join_lobby(engine, endpoint_one, host_session, lobby_id=1, sequence=3)
        _join_lobby(engine, endpoint_two, joiner_session, lobby_id=1, sequence=3)

        room_id = _create_room(engine, endpoint_one, host_session, sequence=4, room_name='join-retry-room')
        join_request = SnapMessage(
            endpoint=endpoint_two,
            type_flags=CHANNEL_ROOM,
            packet_number=0,
            command=commands.CMD_JOIN,
            session_id=joiner_session,
            sequence_number=4,
            acknowledge_number=0,
            payload=struct.pack('>L', room_id),
        )
        join_result = engine.handle_datagram(_encode(join_request), endpoint_two)
        self.assertFalse(join_result.errors)
        self.assertEqual(len(join_result.messages), 2)

        for _ in range(3):
            self.assertEqual(engine.tick(), [])

        retry_messages = engine.tick()
        self.assertEqual(len(retry_messages), 1)
        self.assertEqual(retry_messages[0].command, commands.CMD_JOIN)
        self.assertEqual(retry_messages[0].endpoint, endpoint_one)
        self.assertEqual(retry_messages[0].session_id, host_session)
        self.assertEqual(retry_messages[0].type_flags & FLAG_RELIABLE, 0)

    def test_room_join_callback_retry_continues_after_host_sync_packet(self) -> None:
        config = self._config
        engine = SnapProtocolEngine(config=config, plugin=AutoModellistaPlugin())
        endpoint_one = Endpoint(host='127.0.0.1', port=50122)
        endpoint_two = Endpoint(host='127.0.0.2', port=50123)

        host_session = _create_session_via_login(engine, endpoint_one, 'test')
        joiner_session = _create_session_via_login(engine, endpoint_two, 'test')
        _join_lobby(engine, endpoint_one, host_session, lobby_id=1, sequence=3)
        _join_lobby(engine, endpoint_two, joiner_session, lobby_id=1, sequence=3)

        room_id = _create_room(engine, endpoint_one, host_session, sequence=4, room_name='join-stop-retry-room')
        join_request = SnapMessage(
            endpoint=endpoint_two,
            type_flags=CHANNEL_ROOM,
            packet_number=0,
            command=commands.CMD_JOIN,
            session_id=joiner_session,
            sequence_number=4,
            acknowledge_number=0,
            payload=struct.pack('>L', room_id),
        )
        join_result = engine.handle_datagram(_encode(join_request), endpoint_two)
        self.assertFalse(join_result.errors)
        self.assertEqual(len(join_result.messages), 2)

        host_sync_request = SnapMessage(
            endpoint=endpoint_one,
            type_flags=CHANNEL_ROOM | FLAG_RELIABLE,
            packet_number=0,
            command=commands.CMD_SEND_TARGET,
            session_id=host_session,
            sequence_number=5,
            acknowledge_number=0,
            payload=struct.pack('>LLHL', 1, joiner_session, 0x8005, host_session),
        )
        host_sync_result = engine.handle_datagram(_encode(host_sync_request), endpoint_one)
        self.assertFalse(host_sync_result.errors)
        self.assertEqual(len(host_sync_result.messages), 2)

        for _ in range(3):
            self.assertEqual(engine.tick(), [])

        retry_messages = engine.tick()
        self.assertEqual(len(retry_messages), 1)
        self.assertEqual(retry_messages[0].command, commands.CMD_JOIN)
        self.assertEqual(retry_messages[0].endpoint, endpoint_one)
        self.assertEqual(retry_messages[0].session_id, host_session)

    def test_room_join_callback_retry_stops_after_guest_sync_packet(self) -> None:
        config = self._config
        engine = SnapProtocolEngine(config=config, plugin=AutoModellistaPlugin())
        endpoint_one = Endpoint(host='127.0.0.1', port=50124)
        endpoint_two = Endpoint(host='127.0.0.2', port=50125)

        host_session = _create_session_via_login(engine, endpoint_one, 'test')
        joiner_session = _create_session_via_login(engine, endpoint_two, 'test')
        _join_lobby(engine, endpoint_one, host_session, lobby_id=1, sequence=3)
        _join_lobby(engine, endpoint_two, joiner_session, lobby_id=1, sequence=3)

        room_id = _create_room(engine, endpoint_one, host_session, sequence=4, room_name='join-stop-on-guest-sync')
        join_request = SnapMessage(
            endpoint=endpoint_two,
            type_flags=CHANNEL_ROOM,
            packet_number=0,
            command=commands.CMD_JOIN,
            session_id=joiner_session,
            sequence_number=4,
            acknowledge_number=0,
            payload=struct.pack('>L', room_id),
        )
        join_result = engine.handle_datagram(_encode(join_request), endpoint_two)
        self.assertFalse(join_result.errors)
        self.assertEqual(len(join_result.messages), 2)

        host_sync_request = SnapMessage(
            endpoint=endpoint_one,
            type_flags=CHANNEL_ROOM | FLAG_RELIABLE,
            packet_number=0,
            command=commands.CMD_SEND_TARGET,
            session_id=host_session,
            sequence_number=5,
            acknowledge_number=0,
            payload=struct.pack('>LLHL', 1, joiner_session, 0x8005, host_session),
        )
        host_sync_result = engine.handle_datagram(_encode(host_sync_request), endpoint_one)
        self.assertFalse(host_sync_result.errors)
        self.assertEqual(len(host_sync_result.messages), 2)

        guest_sync_request = SnapMessage(
            endpoint=endpoint_two,
            type_flags=CHANNEL_ROOM | FLAG_RELIABLE,
            packet_number=0,
            command=commands.CMD_SEND_TARGET,
            session_id=joiner_session,
            sequence_number=5,
            acknowledge_number=0,
            payload=struct.pack('>2LHLB', 1, host_session, 0x8102, joiner_session, 1),
        )
        guest_sync_result = engine.handle_datagram(_encode(guest_sync_request), endpoint_two)
        self.assertFalse(guest_sync_result.errors)
        self.assertEqual(len(guest_sync_result.messages), 2)

        for _ in range(4):
            self.assertEqual(engine.tick(), [])

    def test_room_leave_notifies_remaining_members(self) -> None:
        config = self._config
        engine = SnapProtocolEngine(config=config, plugin=AutoModellistaPlugin())
        endpoint_one = Endpoint(host='127.0.0.1', port=50112)
        endpoint_two = Endpoint(host='127.0.0.2', port=50113)

        host_session = _create_session_via_login(engine, endpoint_one, 'test')
        leaver_session = _create_session_via_login(engine, endpoint_two, 'test')
        _join_lobby(engine, endpoint_one, host_session, lobby_id=1, sequence=3)
        _join_lobby(engine, endpoint_two, leaver_session, lobby_id=1, sequence=3)

        room_id = _create_room(engine, endpoint_one, host_session, sequence=4, room_name='leave-callback-room')
        _join_room(engine, endpoint_two, leaver_session, room_id=room_id, sequence=4)

        leave_request = SnapMessage(
            endpoint=endpoint_two,
            type_flags=CHANNEL_ROOM,
            packet_number=0,
            command=commands.CMD_LEAVE,
            session_id=leaver_session,
            sequence_number=5,
            acknowledge_number=0,
            payload=b'',
        )
        leave_result = engine.handle_datagram(_encode(leave_request), endpoint_two)
        self.assertFalse(leave_result.errors)
        self.assertEqual(len(leave_result.messages), 2)

        result_wrapper = leave_result.messages[0]
        callback = leave_result.messages[1]
        self.assertEqual(result_wrapper.command, commands.CMD_RESULT_WRAPPER)
        self.assertEqual(result_wrapper.endpoint, endpoint_two)
        self.assertEqual(struct.unpack_from('>2L', result_wrapper.payload), (0x07, 0))

        self.assertEqual(callback.command, commands.CMD_LEAVE)
        self.assertEqual(callback.endpoint, endpoint_one)
        self.assertEqual(callback.session_id, host_session)
        self.assertEqual(callback.type_flags & FLAG_RESPONSE, FLAG_RESPONSE)
        self.assertEqual(callback.type_flags & FLAG_RELIABLE, 0)
        self.assertEqual(struct.unpack('>L', callback.payload)[0], leaver_session)

    def test_host_room_leave_after_post_game_transition_keeps_normal_leave_flow(self) -> None:
        config = self._config
        engine = SnapProtocolEngine(config=config, plugin=AutoModellistaPlugin())
        endpoint_one = Endpoint(host='127.0.0.1', port=50116)
        endpoint_two = Endpoint(host='127.0.0.2', port=50117)

        host_session = _create_session_via_login(engine, endpoint_one, 'test')
        guest_session = _create_session_via_login(engine, endpoint_two, 'test')
        _join_lobby(engine, endpoint_one, host_session, lobby_id=1, sequence=3)
        _join_lobby(engine, endpoint_two, guest_session, lobby_id=1, sequence=3)

        room_id = _create_room(engine, endpoint_one, host_session, sequence=4, room_name='post-game-leave-room')
        _join_room(engine, endpoint_two, guest_session, room_id=room_id, sequence=4)

        engine.handle_datagram(
            _encode(
                SnapMessage(
                    endpoint=endpoint_one,
                    type_flags=CHANNEL_ROOM | FLAG_RELIABLE,
                    packet_number=0,
                    command=commands.CMD_SEND,
                    session_id=host_session,
                    sequence_number=5,
                    acknowledge_number=0,
                    payload=struct.pack('>H', 0x8001),
                )
            ),
            endpoint_one,
        )

        guest_finish_payload = struct.pack('>H', 0x1469) + b'\x00\x00\x17\x74\x01\x58\x82\x73\x01\x00\x82\x73\x01\x00\x97\x12\x4e\x43'
        guest_finish_detail = SnapMessage(
            endpoint=endpoint_two,
            type_flags=CHANNEL_ROOM | FLAG_RELIABLE,
            packet_number=0,
            command=commands.CMD_SEND,
            session_id=guest_session,
            sequence_number=5,
            acknowledge_number=0,
            payload=guest_finish_payload,
        )
        engine.handle_datagram(_encode(guest_finish_detail), endpoint_two)

        host_finish_payload = struct.pack('>H', 0x1468) + b'\x00\x00\x17\x74\x01\x58\x82\x73\x01\x00\x82\x73\x01\x00\x97\x12\x4e\x43'
        host_finish_detail = SnapMessage(
            endpoint=endpoint_one,
            type_flags=CHANNEL_ROOM | FLAG_RELIABLE,
            packet_number=0,
            command=commands.CMD_SEND,
            session_id=host_session,
            sequence_number=6,
            acknowledge_number=0,
            payload=host_finish_payload,
        )
        engine.handle_datagram(_encode(host_finish_detail), endpoint_one)

        guest_finish = SnapMessage(
            endpoint=endpoint_two,
            type_flags=CHANNEL_ROOM | FLAG_RELIABLE,
            packet_number=0,
            command=commands.CMD_SEND,
            session_id=guest_session,
            sequence_number=6,
            acknowledge_number=0,
            payload=struct.pack('>H', 0x0659) + b'\x00\x00\x26\x9a',
        )
        engine.handle_datagram(_encode(guest_finish), endpoint_two)

        host_finish = SnapMessage(
            endpoint=endpoint_one,
            type_flags=CHANNEL_ROOM | FLAG_RELIABLE,
            packet_number=0,
            command=commands.CMD_SEND,
            session_id=host_session,
            sequence_number=7,
            acknowledge_number=0,
            payload=struct.pack('>H', 0x0658) + b'\x00\x00\x41\x91',
        )
        engine.handle_datagram(_encode(host_finish), endpoint_one)

        leave_request = SnapMessage(
            endpoint=endpoint_one,
            type_flags=CHANNEL_ROOM | FLAG_RELIABLE,
            packet_number=0,
            command=commands.CMD_LEAVE,
            session_id=host_session,
            sequence_number=8,
            acknowledge_number=0,
            payload=b'',
        )
        leave_result = engine.handle_datagram(_encode(leave_request), endpoint_one)
        self.assertFalse(leave_result.errors)
        self.assertEqual(len(leave_result.messages), 2)

        result_wrapper = leave_result.messages[0]
        leave_callback = leave_result.messages[1]
        self.assertEqual(result_wrapper.command, commands.CMD_RESULT_WRAPPER)
        self.assertEqual(struct.unpack_from('>2L', result_wrapper.payload), (0x07, 0))

        self.assertEqual(leave_callback.command, commands.CMD_LEAVE)
        self.assertEqual(leave_callback.endpoint, endpoint_two)
        self.assertEqual(struct.unpack('>L', leave_callback.payload)[0], host_session)
        self.assertEqual(leave_callback.type_flags & FLAG_RELIABLE, 0)

        # Post-game room leave still uses the normal leave selector.
        # Switching this wrapper to `0x06` routes the guest into the
        # join-game-room callback path ("Getting information").
        guest_leave_request = SnapMessage(
            endpoint=endpoint_two,
            type_flags=CHANNEL_ROOM | FLAG_RELIABLE,
            packet_number=0,
            command=commands.CMD_LEAVE,
            session_id=guest_session,
            sequence_number=7,
            acknowledge_number=0,
            payload=b'',
        )
        guest_leave_result = engine.handle_datagram(_encode(guest_leave_request), endpoint_two)
        self.assertFalse(guest_leave_result.errors)
        self.assertGreaterEqual(len(guest_leave_result.messages), 1)
        self.assertEqual(guest_leave_result.messages[0].command, commands.CMD_RESULT_WRAPPER)
        self.assertEqual(
            struct.unpack_from('>2L', guest_leave_result.messages[0].payload),
            (0x07, 0),
        )

    def test_guest_room_leave_after_post_game_transition_keeps_normal_leave_flow(self) -> None:
        config = self._config
        engine = SnapProtocolEngine(config=config, plugin=AutoModellistaPlugin())
        endpoint_one = Endpoint(host='127.0.0.1', port=50118)
        endpoint_two = Endpoint(host='127.0.0.2', port=50119)

        host_session = _create_session_via_login(engine, endpoint_one, 'test')
        guest_session = _create_session_via_login(engine, endpoint_two, 'test')
        _join_lobby(engine, endpoint_one, host_session, lobby_id=1, sequence=3)
        _join_lobby(engine, endpoint_two, guest_session, lobby_id=1, sequence=3)

        room_id = _create_room(engine, endpoint_one, host_session, sequence=4, room_name='post-game-guest-leave-room')
        _join_room(engine, endpoint_two, guest_session, room_id=room_id, sequence=4)

        engine.handle_datagram(
            _encode(
                SnapMessage(
                    endpoint=endpoint_one,
                    type_flags=CHANNEL_ROOM | FLAG_RELIABLE,
                    packet_number=0,
                    command=commands.CMD_SEND,
                    session_id=host_session,
                    sequence_number=5,
                    acknowledge_number=0,
                    payload=struct.pack('>H', 0x8001),
                )
            ),
            endpoint_one,
        )

        guest_finish_payload = struct.pack('>H', 0x1469) + b'\x00\x00\x17\x74\x01\x58\x82\x73\x01\x00\x82\x73\x01\x00\x97\x12\x4e\x43'
        host_finish_payload = struct.pack('>H', 0x1468) + b'\x00\x00\x17\x74\x01\x58\x82\x73\x01\x00\x82\x73\x01\x00\x97\x12\x4e\x43'
        engine.handle_datagram(
            _encode(
                SnapMessage(
                    endpoint=endpoint_two,
                    type_flags=CHANNEL_ROOM | FLAG_RELIABLE,
                    packet_number=0,
                    command=commands.CMD_SEND,
                    session_id=guest_session,
                    sequence_number=5,
                    acknowledge_number=0,
                    payload=guest_finish_payload,
                )
            ),
            endpoint_two,
        )
        engine.handle_datagram(
            _encode(
                SnapMessage(
                    endpoint=endpoint_one,
                    type_flags=CHANNEL_ROOM | FLAG_RELIABLE,
                    packet_number=0,
                    command=commands.CMD_SEND,
                    session_id=host_session,
                    sequence_number=6,
                    acknowledge_number=0,
                    payload=host_finish_payload,
                )
            ),
            endpoint_one,
        )
        engine.handle_datagram(
            _encode(
                SnapMessage(
                    endpoint=endpoint_two,
                    type_flags=CHANNEL_ROOM | FLAG_RELIABLE,
                    packet_number=0,
                    command=commands.CMD_SEND,
                    session_id=guest_session,
                    sequence_number=6,
                    acknowledge_number=0,
                    payload=struct.pack('>H', 0x0659) + b'\x00\x00\x26\x9a',
                )
            ),
            endpoint_two,
        )
        engine.handle_datagram(
            _encode(
                SnapMessage(
                    endpoint=endpoint_one,
                    type_flags=CHANNEL_ROOM | FLAG_RELIABLE,
                    packet_number=0,
                    command=commands.CMD_SEND,
                    session_id=host_session,
                    sequence_number=7,
                    acknowledge_number=0,
                    payload=struct.pack('>H', 0x0658) + b'\x00\x00\x41\x91',
                )
            ),
            endpoint_one,
        )

        leave_request = SnapMessage(
            endpoint=endpoint_two,
            type_flags=CHANNEL_ROOM | FLAG_RELIABLE,
            packet_number=0,
            command=commands.CMD_LEAVE,
            session_id=guest_session,
            sequence_number=7,
            acknowledge_number=0,
            payload=b'',
        )
        leave_result = engine.handle_datagram(_encode(leave_request), endpoint_two)
        self.assertFalse(leave_result.errors)
        self.assertEqual(len(leave_result.messages), 2)

        result_wrapper = leave_result.messages[0]
        leave_callback = leave_result.messages[1]
        self.assertEqual(result_wrapper.command, commands.CMD_RESULT_WRAPPER)
        self.assertEqual(struct.unpack_from('>2L', result_wrapper.payload), (0x07, 0))

        self.assertEqual(leave_callback.command, commands.CMD_LEAVE)
        self.assertEqual(leave_callback.endpoint, endpoint_one)
        self.assertEqual(struct.unpack('>L', leave_callback.payload)[0], guest_session)
        self.assertEqual(leave_callback.type_flags & FLAG_RELIABLE, 0)

    def test_lobby_leave_notifies_remaining_room_members(self) -> None:
        config = self._config
        engine = SnapProtocolEngine(config=config, plugin=AutoModellistaPlugin())
        endpoint_one = Endpoint(host='127.0.0.1', port=50114)
        endpoint_two = Endpoint(host='127.0.0.2', port=50115)

        host_session = _create_session_via_login(engine, endpoint_one, 'test')
        leaver_session = _create_session_via_login(engine, endpoint_two, 'test')
        _join_lobby(engine, endpoint_one, host_session, lobby_id=1, sequence=3)
        _join_lobby(engine, endpoint_two, leaver_session, lobby_id=1, sequence=3)

        room_id = _create_room(engine, endpoint_one, host_session, sequence=4, room_name='lobby-leave-callback-room')
        _join_room(engine, endpoint_two, leaver_session, room_id=room_id, sequence=4)

        leave_request = SnapMessage(
            endpoint=endpoint_two,
            type_flags=CHANNEL_LOBBY,
            packet_number=0,
            command=commands.CMD_LEAVE,
            session_id=leaver_session,
            sequence_number=5,
            acknowledge_number=0,
            payload=b'',
        )
        leave_result = engine.handle_datagram(_encode(leave_request), endpoint_two)
        self.assertFalse(leave_result.errors)
        self.assertEqual(len(leave_result.messages), 2)

        result_wrapper = leave_result.messages[0]
        callback = leave_result.messages[1]
        self.assertEqual(result_wrapper.command, commands.CMD_RESULT_WRAPPER)
        self.assertEqual(result_wrapper.endpoint, endpoint_two)
        self.assertEqual(struct.unpack_from('>2L', result_wrapper.payload), (0x07, 0))

        self.assertEqual(callback.command, commands.CMD_LEAVE)
        self.assertEqual(callback.endpoint, endpoint_one)
        self.assertEqual(callback.session_id, host_session)
        self.assertEqual(callback.type_flags & FLAG_RESPONSE, FLAG_RESPONSE)
        self.assertEqual(callback.type_flags & FLAG_RELIABLE, 0)
        self.assertEqual(struct.unpack('>L', callback.payload)[0], leaver_session)

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

    def test_send_subcommand_8001_broadcasts_to_all_room_members(self) -> None:
        config = self._config
        engine = SnapProtocolEngine(config=config, plugin=AutoModellistaPlugin())
        endpoint_one = Endpoint(host='127.0.0.1', port=50090)
        endpoint_two = Endpoint(host='127.0.0.2', port=50091)

        sender_session = _create_session_via_login(engine, endpoint_one, 'test')
        receiver_session = _create_session_via_login(engine, endpoint_two, 'test')
        _join_lobby(engine, endpoint_one, sender_session, lobby_id=1, sequence=3)
        _join_lobby(engine, endpoint_two, receiver_session, lobby_id=1, sequence=3)

        room_id = _create_room(engine, endpoint_one, sender_session, sequence=4, room_name='room-start')
        _join_room(engine, endpoint_two, receiver_session, room_id=room_id, sequence=4)

        game_request = SnapMessage(
            endpoint=endpoint_one,
            type_flags=CHANNEL_ROOM | FLAG_RELIABLE,
            packet_number=0,
            command=commands.CMD_SEND,
            session_id=sender_session,
            sequence_number=5,
            acknowledge_number=0,
            payload=struct.pack('>H', 0x8001),
        )
        game_result = engine.handle_datagram(_encode(game_request), endpoint_one)
        self.assertFalse(game_result.errors)
        self.assertEqual(len(game_result.messages), 3)

        ack = game_result.messages[0]
        relays = game_result.messages[1:]
        self.assertEqual(ack.command, commands.CMD_ACK)
        self.assertEqual(ack.endpoint, endpoint_one)
        self.assertTrue(all(message.command == commands.CMD_SEND for message in relays))
        self.assertEqual({message.endpoint for message in relays}, {endpoint_one, endpoint_two})
        self.assertEqual({message.session_id for message in relays}, {sender_session, receiver_session})
        self.assertTrue(all(message.payload == game_request.payload for message in relays))
        self.assertTrue(all((message.type_flags & FLAG_MULTI) == 0 for message in relays))

    def test_send_subcommand_0658_relays_to_other_room_members_only(self) -> None:
        config = self._config
        engine = SnapProtocolEngine(config=config, plugin=AutoModellistaPlugin())
        endpoint_one = Endpoint(host='127.0.0.1', port=50098)
        endpoint_two = Endpoint(host='127.0.0.2', port=50099)

        sender_session = _create_session_via_login(engine, endpoint_one, 'test')
        receiver_session = _create_session_via_login(engine, endpoint_two, 'test')
        _join_lobby(engine, endpoint_one, sender_session, lobby_id=1, sequence=3)
        _join_lobby(engine, endpoint_two, receiver_session, lobby_id=1, sequence=3)

        room_id = _create_room(engine, endpoint_one, sender_session, sequence=4, room_name='room-finish')
        _join_room(engine, endpoint_two, receiver_session, room_id=room_id, sequence=4)

        game_request = SnapMessage(
            endpoint=endpoint_one,
            type_flags=CHANNEL_ROOM | FLAG_RELIABLE,
            packet_number=0,
            command=commands.CMD_SEND,
            session_id=sender_session,
            sequence_number=5,
            acknowledge_number=0,
            payload=struct.pack('>H', 0x0658) + b'\x00\x00\x41\x91',
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
        self.assertEqual(relay.type_flags & FLAG_MULTI, 0)

    def test_end_of_game_reports_emit_room_transition_8009_once_all_members_finish(self) -> None:
        config = self._config
        engine = SnapProtocolEngine(config=config, plugin=AutoModellistaPlugin())
        endpoint_one = Endpoint(host='127.0.0.1', port=50150)
        endpoint_two = Endpoint(host='127.0.0.2', port=50151)

        sender_session = _create_session_via_login(engine, endpoint_one, 'test')
        receiver_session = _create_session_via_login(engine, endpoint_two, 'test')
        _join_lobby(engine, endpoint_one, sender_session, lobby_id=1, sequence=3)
        _join_lobby(engine, endpoint_two, receiver_session, lobby_id=1, sequence=3)

        room_id = _create_room(engine, endpoint_one, sender_session, sequence=4, room_name='room-post-game')
        _join_room(engine, endpoint_two, receiver_session, room_id=room_id, sequence=4)

        engine.handle_datagram(
            _encode(
                SnapMessage(
                    endpoint=endpoint_one,
                    type_flags=CHANNEL_ROOM | FLAG_RELIABLE,
                    packet_number=0,
                    command=commands.CMD_SEND,
                    session_id=sender_session,
                    sequence_number=5,
                    acknowledge_number=0,
                    payload=struct.pack('>H', 0x8001),
                )
            ),
            endpoint_one,
        )

        first_finish_detail = SnapMessage(
            endpoint=endpoint_one,
            type_flags=CHANNEL_ROOM | FLAG_RELIABLE,
            packet_number=0,
            command=commands.CMD_SEND,
            session_id=sender_session,
            sequence_number=6,
            acknowledge_number=0,
            payload=struct.pack('>H', 0x1468) + b'\x00\x00\x17\x74\x01\x58\x82\x73\x01\x00\x82\x73\x01\x00\x97\x12\x4e\x43',
        )
        first_result = engine.handle_datagram(_encode(first_finish_detail), endpoint_one)
        self.assertFalse(first_result.errors)
        self.assertEqual(len(first_result.messages), 2)

        second_finish_detail = SnapMessage(
            endpoint=endpoint_two,
            type_flags=CHANNEL_ROOM | FLAG_RELIABLE,
            packet_number=0,
            command=commands.CMD_SEND,
            session_id=receiver_session,
            sequence_number=5,
            acknowledge_number=0,
            payload=struct.pack('>H', 0x1469) + b'\x00\x00\x17\x74\x01\x58\x82\x73\x01\x00\x82\x73\x01\x00\x97\x12\x4e\x43',
        )
        second_result = engine.handle_datagram(_encode(second_finish_detail), endpoint_two)
        self.assertFalse(second_result.errors)
        self.assertEqual(len(second_result.messages), 2)

        first_finish = SnapMessage(
            endpoint=endpoint_one,
            type_flags=CHANNEL_ROOM | FLAG_RELIABLE,
            packet_number=0,
            command=commands.CMD_SEND,
            session_id=sender_session,
            sequence_number=7,
            acknowledge_number=0,
            payload=struct.pack('>H', 0x0658) + b'\x00\x00\x41\x91',
        )
        third_result = engine.handle_datagram(_encode(first_finish), endpoint_one)
        self.assertFalse(third_result.errors)
        self.assertEqual(len(third_result.messages), 2)

        second_finish = SnapMessage(
            endpoint=endpoint_two,
            type_flags=CHANNEL_ROOM | FLAG_RELIABLE,
            packet_number=0,
            command=commands.CMD_SEND,
            session_id=receiver_session,
            sequence_number=6,
            acknowledge_number=0,
            payload=struct.pack('>H', 0x0659) + b'\x00\x00\x26\x9a',
        )
        fourth_result = engine.handle_datagram(_encode(second_finish), endpoint_two)
        second_result = fourth_result
        self.assertFalse(second_result.errors)
        self.assertEqual(len(second_result.messages), 4)

        ack = second_result.messages[0]
        relays = second_result.messages[1:2]
        transitions = second_result.messages[2:]
        self.assertEqual(ack.command, commands.CMD_ACK)
        self.assertEqual(ack.endpoint, endpoint_two)
        self.assertTrue(all(message.command == commands.CMD_SEND for message in relays))
        self.assertEqual({message.endpoint for message in relays}, {endpoint_one})
        self.assertEqual(relays[0].session_id, sender_session)
        self.assertTrue(all(message.payload == second_finish.payload for message in relays))

        self.assertTrue(all(message.command == commands.CMD_SEND for message in transitions))
        self.assertEqual({message.endpoint for message in transitions}, {endpoint_one, endpoint_two})
        self.assertEqual({message.session_id for message in transitions}, {sender_session, receiver_session})
        self.assertTrue(all(message.payload == struct.pack('>H', 0x8009) for message in transitions))
        self.assertTrue(
            all((message.type_flags & (CHANNEL_ROOM | FLAG_RELIABLE)) == (CHANNEL_ROOM | FLAG_RELIABLE)
                for message in transitions)
        )

    def test_end_of_game_reports_do_not_emit_room_transition_before_game_start(self) -> None:
        config = self._config
        engine = SnapProtocolEngine(config=config, plugin=AutoModellistaPlugin())
        endpoint_one = Endpoint(host='127.0.0.1', port=50152)
        endpoint_two = Endpoint(host='127.0.0.2', port=50153)

        sender_session = _create_session_via_login(engine, endpoint_one, 'test')
        receiver_session = _create_session_via_login(engine, endpoint_two, 'test')
        _join_lobby(engine, endpoint_one, sender_session, lobby_id=1, sequence=3)
        _join_lobby(engine, endpoint_two, receiver_session, lobby_id=1, sequence=3)

        room_id = _create_room(engine, endpoint_one, sender_session, sequence=4, room_name='room-post-game-no-start')
        _join_room(engine, endpoint_two, receiver_session, room_id=room_id, sequence=4)

        engine.handle_datagram(
            _encode(
                SnapMessage(
                    endpoint=endpoint_one,
                    type_flags=CHANNEL_ROOM | FLAG_RELIABLE,
                    packet_number=0,
                    command=commands.CMD_SEND,
                    session_id=sender_session,
                    sequence_number=5,
                    acknowledge_number=0,
                    payload=struct.pack('>H', 0x1468) + b'\x00\x00\x17\x74\x01\x58\x82\x73\x01\x00\x82\x73\x01\x00\x97\x12\x4e\x43',
                )
            ),
            endpoint_one,
        )
        engine.handle_datagram(
            _encode(
                SnapMessage(
                    endpoint=endpoint_two,
                    type_flags=CHANNEL_ROOM | FLAG_RELIABLE,
                    packet_number=0,
                    command=commands.CMD_SEND,
                    session_id=receiver_session,
                    sequence_number=5,
                    acknowledge_number=0,
                    payload=struct.pack('>H', 0x1469) + b'\x00\x00\x17\x74\x01\x58\x82\x73\x01\x00\x82\x73\x01\x00\x97\x12\x4e\x43',
                )
            ),
            endpoint_two,
        )
        engine.handle_datagram(
            _encode(
                SnapMessage(
                    endpoint=endpoint_one,
                    type_flags=CHANNEL_ROOM | FLAG_RELIABLE,
                    packet_number=0,
                    command=commands.CMD_SEND,
                    session_id=sender_session,
                    sequence_number=6,
                    acknowledge_number=0,
                    payload=struct.pack('>H', 0x0658) + b'\x00\x00\x41\x91',
                )
            ),
            endpoint_one,
        )
        result = engine.handle_datagram(
            _encode(
                SnapMessage(
                    endpoint=endpoint_two,
                    type_flags=CHANNEL_ROOM | FLAG_RELIABLE,
                    packet_number=0,
                    command=commands.CMD_SEND,
                    session_id=receiver_session,
                    sequence_number=6,
                    acknowledge_number=0,
                    payload=struct.pack('>H', 0x0659) + b'\x00\x00\x26\x9a',
                )
            ),
            endpoint_two,
        )

        self.assertFalse(result.errors)
        self.assertEqual(len(result.messages), 2)
        self.assertEqual(result.messages[0].command, commands.CMD_ACK)
        self.assertEqual(result.messages[1].command, commands.CMD_SEND)
        self.assertEqual(result.messages[1].endpoint, endpoint_one)
        self.assertEqual(result.messages[1].payload, struct.pack('>H', 0x0659) + b'\x00\x00\x26\x9a')

    def test_send_unknown_subcommand_relays_to_other_room_members(self) -> None:
        config = self._config
        engine = SnapProtocolEngine(config=config, plugin=AutoModellistaPlugin())
        endpoint_one = Endpoint(host='127.0.0.1', port=50094)
        endpoint_two = Endpoint(host='127.0.0.2', port=50095)

        sender_session = _create_session_via_login(engine, endpoint_one, 'test')
        receiver_session = _create_session_via_login(engine, endpoint_two, 'test')
        _join_lobby(engine, endpoint_one, sender_session, lobby_id=1, sequence=3)
        _join_lobby(engine, endpoint_two, receiver_session, lobby_id=1, sequence=3)

        room_id = _create_room(engine, endpoint_one, sender_session, sequence=4, room_name='room-unknown')
        _join_room(engine, endpoint_two, receiver_session, room_id=room_id, sequence=4)

        game_request = SnapMessage(
            endpoint=endpoint_one,
            type_flags=CHANNEL_ROOM | FLAG_RELIABLE,
            packet_number=0,
            command=commands.CMD_SEND,
            session_id=sender_session,
            sequence_number=5,
            acknowledge_number=0,
            payload=struct.pack('>H', 0x0648) + b'\x00\x00\x01\x80',
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

    def test_multi_send_does_not_force_leave_room(self) -> None:
        config = self._config
        engine = SnapProtocolEngine(config=config, plugin=AutoModellistaPlugin())
        endpoint = Endpoint(host='127.0.0.1', port=50032)

        session_id = _create_session_via_login(engine, endpoint, 'test')
        _join_lobby(engine, endpoint, session_id, lobby_id=1, sequence=3)
        room_id = _create_room(engine, endpoint, session_id, sequence=4, room_name='room-multi')

        request = SnapMessage(
            endpoint=endpoint,
            type_flags=CHANNEL_ROOM | FLAG_MULTI | FLAG_RELIABLE,
            packet_number=0,
            command=commands.CMD_SEND,
            session_id=session_id,
            sequence_number=5,
            acknowledge_number=0,
            payload=b'\x80\x01',
        )
        result = engine.handle_datagram(_encode(request), endpoint)
        self.assertFalse(result.errors)
        self.assertEqual(len(result.messages), 2)
        self.assertEqual(result.messages[0].command, commands.CMD_ACK)
        self.assertEqual(result.messages[0].endpoint, endpoint)
        self.assertEqual(result.messages[0].session_id, session_id)
        self.assertEqual(result.messages[1].command, commands.CMD_SEND)
        self.assertEqual(result.messages[1].endpoint, endpoint)
        self.assertEqual(result.messages[1].session_id, session_id)
        self.assertEqual(result.messages[1].payload, request.payload)

        session = engine._sessions.get(session_id)  # noqa: SLF001
        self.assertIsNotNone(session)
        self.assertEqual(session.room_id, room_id)

    def test_multi_send_subcommand_8001_broadcasts_to_all_room_members(self) -> None:
        config = self._config
        engine = SnapProtocolEngine(config=config, plugin=AutoModellistaPlugin())
        endpoint_one = Endpoint(host='127.0.0.1', port=50035)
        endpoint_two = Endpoint(host='127.0.0.2', port=50036)

        sender_session = _create_session_via_login(engine, endpoint_one, 'test')
        receiver_session = _create_session_via_login(engine, endpoint_two, 'test')
        _join_lobby(engine, endpoint_one, sender_session, lobby_id=1, sequence=3)
        _join_lobby(engine, endpoint_two, receiver_session, lobby_id=1, sequence=3)

        room_id = _create_room(engine, endpoint_one, sender_session, sequence=4, room_name='room-start-multi')
        _join_room(engine, endpoint_two, receiver_session, room_id=room_id, sequence=4)

        outer = SnapMessage(
            endpoint=endpoint_one,
            type_flags=CHANNEL_ROOM | FLAG_MULTI | FLAG_RELIABLE,
            packet_number=0,
            command=commands.CMD_SEND,
            session_id=sender_session,
            sequence_number=5,
            acknowledge_number=0,
            payload=b'\x80\x01',
            size_word_override=(CHANNEL_ROOM | FLAG_MULTI | FLAG_RELIABLE) | 0x0012,
        )
        embedded = SnapMessage(
            endpoint=endpoint_one,
            type_flags=CHANNEL_ROOM | FLAG_RELIABLE,
            packet_number=0,
            command=commands.CMD_CHANGE_ATTRIBUTE,
            session_id=sender_session,
            sequence_number=0,
            acknowledge_number=0,
            payload=b'STAT\xc0\x00\x00\x00',
        )
        result = engine.handle_datagram(_encode_many([outer, embedded]), endpoint_one)
        self.assertFalse(result.errors)
        self.assertEqual(len(result.messages), 4)

        ack = result.messages[0]
        relays = result.messages[1:3]
        change = result.messages[3]
        self.assertEqual(ack.command, commands.CMD_ACK)
        self.assertEqual(ack.endpoint, endpoint_one)
        self.assertEqual(ack.acknowledge_number, 5)
        self.assertTrue(all(message.command == commands.CMD_SEND for message in relays))
        self.assertEqual({message.endpoint for message in relays}, {endpoint_one, endpoint_two})
        self.assertEqual({message.session_id for message in relays}, {sender_session, receiver_session})
        self.assertTrue(all(message.payload == outer.payload for message in relays))
        self.assertTrue(all((message.type_flags & FLAG_MULTI) == 0 for message in relays))
        self.assertEqual(change.command, commands.CMD_RESULT_WRAPPER)
        self.assertEqual(change.acknowledge_number, 5)
        self.assertEqual(struct.unpack_from('>2L', change.payload), (0x08, 0))

    def test_multi_send_subcommand_1468_relays_to_other_room_members_only(self) -> None:
        config = self._config
        engine = SnapProtocolEngine(config=config, plugin=AutoModellistaPlugin())
        endpoint_one = Endpoint(host='127.0.0.1', port=50100)
        endpoint_two = Endpoint(host='127.0.0.2', port=50101)

        sender_session = _create_session_via_login(engine, endpoint_one, 'test')
        receiver_session = _create_session_via_login(engine, endpoint_two, 'test')
        _join_lobby(engine, endpoint_one, sender_session, lobby_id=1, sequence=3)
        _join_lobby(engine, endpoint_two, receiver_session, lobby_id=1, sequence=3)

        room_id = _create_room(engine, endpoint_one, sender_session, sequence=4, room_name='room-finish-multi')
        _join_room(engine, endpoint_two, receiver_session, room_id=room_id, sequence=4)

        request = SnapMessage(
            endpoint=endpoint_one,
            type_flags=CHANNEL_ROOM | FLAG_MULTI | FLAG_RELIABLE,
            packet_number=0,
            command=commands.CMD_SEND,
            session_id=sender_session,
            sequence_number=5,
            acknowledge_number=0,
            payload=struct.pack('>H', 0x1468) + b'\x00\x00\x41\x84',
            size_word_override=(CHANNEL_ROOM | FLAG_MULTI | FLAG_RELIABLE) | 0x0016,
        )
        result = engine.handle_datagram(_encode(request), endpoint_one)
        self.assertFalse(result.errors)
        self.assertEqual(len(result.messages), 2)

        ack = result.messages[0]
        relay = result.messages[1]
        self.assertEqual(ack.command, commands.CMD_ACK)
        self.assertEqual(ack.endpoint, endpoint_one)
        self.assertEqual(relay.command, commands.CMD_SEND)
        self.assertEqual(relay.endpoint, endpoint_two)
        self.assertEqual(relay.session_id, receiver_session)
        self.assertEqual(relay.payload, request.payload)
        self.assertEqual(relay.type_flags & FLAG_MULTI, 0)

    def test_multi_send_unknown_subcommand_relays_to_other_room_members(self) -> None:
        config = self._config
        engine = SnapProtocolEngine(config=config, plugin=AutoModellistaPlugin())
        endpoint_one = Endpoint(host='127.0.0.1', port=50096)
        endpoint_two = Endpoint(host='127.0.0.2', port=50097)

        sender_session = _create_session_via_login(engine, endpoint_one, 'test')
        receiver_session = _create_session_via_login(engine, endpoint_two, 'test')
        _join_lobby(engine, endpoint_one, sender_session, lobby_id=1, sequence=3)
        _join_lobby(engine, endpoint_two, receiver_session, lobby_id=1, sequence=3)

        room_id = _create_room(engine, endpoint_one, sender_session, sequence=4, room_name='room-unknown-multi')
        _join_room(engine, endpoint_two, receiver_session, room_id=room_id, sequence=4)

        request = SnapMessage(
            endpoint=endpoint_one,
            type_flags=CHANNEL_ROOM | FLAG_MULTI | FLAG_RELIABLE,
            packet_number=0,
            command=commands.CMD_SEND,
            session_id=sender_session,
            sequence_number=5,
            acknowledge_number=0,
            payload=struct.pack('>H', 0x0648) + b'\x00\x00\x00@',
            size_word_override=(CHANNEL_ROOM | FLAG_MULTI | FLAG_RELIABLE) | 0x0016,
        )
        result = engine.handle_datagram(_encode(request), endpoint_one)
        self.assertFalse(result.errors)
        self.assertEqual(len(result.messages), 2)

        ack = result.messages[0]
        relay = result.messages[1]
        self.assertEqual(ack.command, commands.CMD_ACK)
        self.assertEqual(ack.endpoint, endpoint_one)
        self.assertEqual(relay.command, commands.CMD_SEND)
        self.assertEqual(relay.endpoint, endpoint_two)
        self.assertEqual(relay.session_id, receiver_session)
        self.assertEqual(relay.payload, request.payload)
        self.assertEqual(relay.type_flags & FLAG_MULTI, 0)

    def test_multi_send_embedded_seq_zero_send_is_dispatched(self) -> None:
        config = self._config
        engine = SnapProtocolEngine(config=config, plugin=AutoModellistaPlugin())
        endpoint_one = Endpoint(host='127.0.0.1', port=50120)
        endpoint_two = Endpoint(host='127.0.0.2', port=50121)

        sender_session = _create_session_via_login(engine, endpoint_one, 'test')
        receiver_session = _create_session_via_login(engine, endpoint_two, 'test')
        _join_lobby(engine, endpoint_one, sender_session, lobby_id=1, sequence=3)
        _join_lobby(engine, endpoint_two, receiver_session, lobby_id=1, sequence=3)

        room_id = _create_room(engine, endpoint_one, sender_session, sequence=4, room_name='room-embedded-seq0')
        _join_room(engine, endpoint_two, receiver_session, room_id=room_id, sequence=4)

        outer = SnapMessage(
            endpoint=endpoint_one,
            type_flags=CHANNEL_ROOM | FLAG_MULTI | FLAG_RELIABLE,
            packet_number=0,
            command=commands.CMD_SEND,
            session_id=sender_session,
            sequence_number=11,
            acknowledge_number=0,
            payload=bytes.fromhex('064900000880'),
            size_word_override=(CHANNEL_ROOM | FLAG_MULTI | FLAG_RELIABLE) | 0x0016,
        )
        embedded = SnapMessage(
            endpoint=endpoint_one,
            type_flags=CHANNEL_ROOM | FLAG_RELIABLE,
            packet_number=0,
            command=commands.CMD_SEND,
            session_id=sender_session,
            sequence_number=0,
            acknowledge_number=0,
            payload=bytes.fromhex(
                '40390000089c1820800c0000000000000000000000000000'
                '000000000000000000000000000000000000000000000000'
                '0000000000000000000000000000'
            ),
        )

        result = engine.handle_datagram(_encode_many([outer, embedded]), endpoint_one)
        self.assertFalse(result.errors)

        relays = [
            message for message in result.messages
            if message.command == commands.CMD_SEND and message.endpoint == endpoint_two
        ]
        self.assertEqual(len(relays), 2)
        self.assertEqual(relays[0].payload, outer.payload)
        self.assertEqual(relays[1].payload, embedded.payload)

    def test_multi_send_subcommand_8002_leaves_room(self) -> None:
        config = self._config
        engine = SnapProtocolEngine(config=config, plugin=AutoModellistaPlugin())
        endpoint = Endpoint(host='127.0.0.1', port=50033)

        session_id = _create_session_via_login(engine, endpoint, 'test')
        _join_lobby(engine, endpoint, session_id, lobby_id=1, sequence=3)
        room_id = _create_room(engine, endpoint, session_id, sequence=4, room_name='room-exit')

        outer = SnapMessage(
            endpoint=endpoint,
            type_flags=CHANNEL_ROOM | FLAG_MULTI | FLAG_RELIABLE,
            packet_number=0,
            command=commands.CMD_SEND,
            session_id=session_id,
            sequence_number=5,
            acknowledge_number=0,
            payload=b'\x80\x02',
            size_word_override=(CHANNEL_ROOM | FLAG_MULTI | FLAG_RELIABLE) | 0x0012,
        )
        embedded_leave = SnapMessage(
            endpoint=endpoint,
            type_flags=CHANNEL_ROOM | FLAG_RELIABLE,
            packet_number=0,
            command=commands.CMD_LEAVE,
            session_id=session_id,
            sequence_number=0,
            acknowledge_number=0,
            payload=b'',
        )
        result = engine.handle_datagram(_encode_many([outer, embedded_leave]), endpoint)
        self.assertFalse(result.errors)
        self.assertEqual(len(result.messages), 2)

        ack = result.messages[0]
        leave_result = result.messages[1]
        self.assertEqual(ack.command, commands.CMD_ACK)
        self.assertEqual(ack.endpoint, endpoint)
        self.assertEqual(ack.session_id, session_id)
        self.assertEqual(leave_result.command, commands.CMD_RESULT_WRAPPER)
        self.assertEqual(leave_result.endpoint, endpoint)
        self.assertEqual(leave_result.session_id, session_id)
        self.assertEqual(leave_result.acknowledge_number, 5)
        self.assertEqual(struct.unpack_from('>2L', leave_result.payload), (0x07, 0))

        session = engine._sessions.get(session_id)  # noqa: SLF001
        self.assertIsNotNone(session)
        self.assertEqual(session.room_id, 0)

    def test_multi_send_subcommand_8001_embedded_change_attribute_acks_outer_sequence(self) -> None:
        config = self._config
        engine = SnapProtocolEngine(config=config, plugin=AutoModellistaPlugin())
        endpoint = Endpoint(host='127.0.0.1', port=50034)

        session_id = _create_session_via_login(engine, endpoint, 'test')
        _join_lobby(engine, endpoint, session_id, lobby_id=1, sequence=3)
        _create_room(engine, endpoint, session_id, sequence=4, room_name='room-start')

        outer = SnapMessage(
            endpoint=endpoint,
            type_flags=CHANNEL_ROOM | FLAG_MULTI | FLAG_RELIABLE,
            packet_number=0,
            command=commands.CMD_SEND,
            session_id=session_id,
            sequence_number=5,
            acknowledge_number=0,
            payload=b'\x80\x01',
            size_word_override=(CHANNEL_ROOM | FLAG_MULTI | FLAG_RELIABLE) | 0x0012,
        )
        embedded = SnapMessage(
            endpoint=endpoint,
            type_flags=CHANNEL_ROOM | FLAG_RELIABLE,
            packet_number=0,
            command=commands.CMD_CHANGE_ATTRIBUTE,
            session_id=session_id,
            sequence_number=0,
            acknowledge_number=0,
            payload=b'STAT\xc0\x00\x00\x00',
        )
        result = engine.handle_datagram(_encode_many([outer, embedded]), endpoint)
        self.assertFalse(result.errors)
        self.assertEqual(len(result.messages), 3)

        ack = result.messages[0]
        relay = result.messages[1]
        change = result.messages[2]
        self.assertEqual(ack.command, commands.CMD_ACK)
        self.assertEqual(ack.acknowledge_number, 5)
        self.assertEqual(relay.command, commands.CMD_SEND)
        self.assertEqual(relay.endpoint, endpoint)
        self.assertEqual(relay.session_id, session_id)
        self.assertEqual(relay.payload, outer.payload)
        self.assertEqual(relay.type_flags & FLAG_MULTI, 0)
        self.assertEqual(change.command, commands.CMD_RESULT_WRAPPER)
        self.assertEqual(change.acknowledge_number, 5)
        self.assertEqual(struct.unpack_from('>2L', change.payload), (0x08, 0))

    def test_send_target_subcommand_8001_relays_to_target(self) -> None:
        config = self._config
        engine = SnapProtocolEngine(config=config, plugin=AutoModellistaPlugin())
        endpoint_one = Endpoint(host='127.0.0.1', port=50037)
        endpoint_two = Endpoint(host='127.0.0.2', port=50038)

        sender_session = _create_session_via_login(engine, endpoint_one, 'test')
        receiver_session = _create_session_via_login(engine, endpoint_two, 'test')
        _join_lobby(engine, endpoint_one, sender_session, lobby_id=1, sequence=3)
        _join_lobby(engine, endpoint_two, receiver_session, lobby_id=1, sequence=3)

        room_id = _create_room(engine, endpoint_one, sender_session, sequence=4, room_name='room-target')
        _join_room(engine, endpoint_two, receiver_session, room_id=room_id, sequence=4)

        request = SnapMessage(
            endpoint=endpoint_one,
            type_flags=CHANNEL_ROOM | FLAG_RELIABLE,
            packet_number=0,
            command=commands.CMD_SEND_TARGET,
            session_id=sender_session,
            sequence_number=5,
            acknowledge_number=0,
            payload=struct.pack('>2LH', 1, receiver_session, 0x8001),
        )
        result = engine.handle_datagram(_encode(request), endpoint_one)
        self.assertFalse(result.errors)
        self.assertEqual(len(result.messages), 2)

        ack = result.messages[0]
        relay = result.messages[1]
        self.assertEqual(ack.command, commands.CMD_ACK)
        self.assertEqual(ack.endpoint, endpoint_one)
        self.assertEqual(relay.command, commands.CMD_SEND_TARGET)
        self.assertEqual(relay.endpoint, endpoint_two)
        self.assertEqual(relay.session_id, receiver_session)
        self.assertEqual(struct.unpack('>2LH', relay.payload), (1, 0, 0x8001))
        self.assertEqual(relay.sequence_number, 0)

    def test_first_reliable_send_target_relay_uses_sequence_zero_after_room_join(self) -> None:
        config = self._config
        engine = SnapProtocolEngine(config=config, plugin=AutoModellistaPlugin())
        endpoint_one = Endpoint(host='127.0.0.1', port=50108)
        endpoint_two = Endpoint(host='127.0.0.2', port=50109)

        sender_session = _create_session_via_login(engine, endpoint_one, 'test')
        receiver_session = _create_session_via_login(engine, endpoint_two, 'test')
        _join_lobby(engine, endpoint_one, sender_session, lobby_id=1, sequence=3)
        _join_lobby(engine, endpoint_two, receiver_session, lobby_id=1, sequence=3)

        room_id = _create_room(engine, endpoint_one, sender_session, sequence=4, room_name='first-relay-seq')
        _join_room(engine, endpoint_two, receiver_session, room_id=room_id, sequence=4)

        request = SnapMessage(
            endpoint=endpoint_two,
            type_flags=CHANNEL_ROOM | FLAG_RELIABLE,
            packet_number=0,
            command=commands.CMD_SEND_TARGET,
            session_id=receiver_session,
            sequence_number=12,
            acknowledge_number=0,
            payload=struct.pack('>2LHLB', 1, sender_session, 0x8102, receiver_session, 1),
        )
        result = engine.handle_datagram(_encode(request), endpoint_two)
        self.assertFalse(result.errors)
        self.assertEqual(len(result.messages), 2)
        relay = result.messages[1]
        self.assertEqual(relay.command, commands.CMD_SEND_TARGET)
        self.assertEqual(relay.endpoint, endpoint_one)
        self.assertEqual(relay.sequence_number, 0)

    def test_send_target_subcommand_8103_relays_to_target(self) -> None:
        config = self._config
        engine = SnapProtocolEngine(config=config, plugin=AutoModellistaPlugin())
        endpoint_one = Endpoint(host='127.0.0.1', port=50092)
        endpoint_two = Endpoint(host='127.0.0.2', port=50093)

        sender_session = _create_session_via_login(engine, endpoint_one, 'test')
        receiver_session = _create_session_via_login(engine, endpoint_two, 'test')
        _join_lobby(engine, endpoint_one, sender_session, lobby_id=1, sequence=3)
        _join_lobby(engine, endpoint_two, receiver_session, lobby_id=1, sequence=3)

        room_id = _create_room(engine, endpoint_one, sender_session, sequence=4, room_name='room-target-8103')
        _join_room(engine, endpoint_two, receiver_session, room_id=room_id, sequence=4)

        request = SnapMessage(
            endpoint=endpoint_one,
            type_flags=CHANNEL_ROOM | FLAG_RELIABLE,
            packet_number=0,
            command=commands.CMD_SEND_TARGET,
            session_id=sender_session,
            sequence_number=5,
            acknowledge_number=0,
            payload=struct.pack('>2LHLB', 1, receiver_session, 0x8103, sender_session, 1),
        )
        result = engine.handle_datagram(_encode(request), endpoint_one)
        self.assertFalse(result.errors)
        self.assertEqual(len(result.messages), 2)

        ack = result.messages[0]
        relay = result.messages[1]
        self.assertEqual(ack.command, commands.CMD_ACK)
        self.assertEqual(ack.endpoint, endpoint_one)
        self.assertEqual(relay.command, commands.CMD_SEND_TARGET)
        self.assertEqual(relay.endpoint, endpoint_two)
        self.assertEqual(relay.session_id, receiver_session)
        self.assertEqual(
            struct.unpack('>2LHLB', relay.payload),
            (1, 0, 0x8103, sender_session, 1),
        )

    def test_send_target_unknown_subcommand_relays_with_zeroed_target_slot(self) -> None:
        config = self._config
        engine = SnapProtocolEngine(config=config, plugin=AutoModellistaPlugin())
        endpoint_one = Endpoint(host='127.0.0.1', port=50098)
        endpoint_two = Endpoint(host='127.0.0.2', port=50099)

        sender_session = _create_session_via_login(engine, endpoint_one, 'test')
        receiver_session = _create_session_via_login(engine, endpoint_two, 'test')
        _join_lobby(engine, endpoint_one, sender_session, lobby_id=1, sequence=3)
        _join_lobby(engine, endpoint_two, receiver_session, lobby_id=1, sequence=3)

        room_id = _create_room(engine, endpoint_one, sender_session, sequence=4, room_name='room-target-unknown')
        _join_room(engine, endpoint_two, receiver_session, room_id=room_id, sequence=4)

        request = SnapMessage(
            endpoint=endpoint_one,
            type_flags=CHANNEL_ROOM | FLAG_RELIABLE,
            packet_number=0,
            command=commands.CMD_SEND_TARGET,
            session_id=sender_session,
            sequence_number=5,
            acknowledge_number=0,
            payload=struct.pack('>2LH', 1, receiver_session, 0x9234) + b'\x12\x34\x56\x78',
        )
        result = engine.handle_datagram(_encode(request), endpoint_one)
        self.assertFalse(result.errors)
        self.assertEqual(len(result.messages), 2)

        ack = result.messages[0]
        relay = result.messages[1]
        self.assertEqual(ack.command, commands.CMD_ACK)
        self.assertEqual(ack.endpoint, endpoint_one)
        self.assertEqual(relay.command, commands.CMD_SEND_TARGET)
        self.assertEqual(relay.endpoint, endpoint_two)
        self.assertEqual(relay.session_id, receiver_session)
        expected_payload = request.payload[:4] + struct.pack('>L', 0) + request.payload[8:]
        self.assertEqual(relay.payload, expected_payload)

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

    def test_create_room_reliable_retransmit_reuses_previous_result(self) -> None:
        config = self._config
        engine = SnapProtocolEngine(config=config, plugin=AutoModellistaPlugin())
        endpoint = Endpoint(host='127.0.0.1', port=50041)

        session_id = _create_session_via_login(engine, endpoint, 'test')
        _join_lobby(engine, endpoint, session_id, lobby_id=1, sequence=3)

        request = _build_create_room_request(
            endpoint=endpoint,
            session_id=session_id,
            sequence=4,
            room_name='dup-room',
        )
        first_result = engine.handle_datagram(_encode(request), endpoint)
        self.assertFalse(first_result.errors)
        self.assertEqual(len(first_result.messages), 1)
        _, first_room_id = struct.unpack_from('>2L', first_result.messages[0].payload)
        self.assertGreater(first_room_id, 0)

        second_result = engine.handle_datagram(_encode(request), endpoint)
        self.assertFalse(second_result.errors)
        self.assertEqual(len(second_result.messages), 1)
        _, second_room_id = struct.unpack_from('>2L', second_result.messages[0].payload)
        self.assertEqual(first_room_id, second_room_id)

    def test_query_game_rooms_prunes_members_without_matching_session_room(self) -> None:
        config = self._config
        engine = SnapProtocolEngine(config=config, plugin=AutoModellistaPlugin())
        endpoint = Endpoint(host='127.0.0.1', port=50042)

        session_id = _create_session_via_login(engine, endpoint, 'test')
        _join_lobby(engine, endpoint, session_id, lobby_id=1, sequence=3)
        room_id = _create_room(engine, endpoint, session_id, sequence=4, room_name='stale-room')
        self.assertGreater(room_id, 0)

        # Simulate stale persistent membership where the session no longer points to this room.
        engine._sessions.set_room(session_id, 0)  # noqa: SLF001

        request = SnapMessage(
            endpoint=endpoint,
            type_flags=CHANNEL_LOBBY | FLAG_RELIABLE,
            packet_number=0,
            command=commands.CMD_QUERY_GAME_ROOMS,
            session_id=session_id,
            sequence_number=5,
            acknowledge_number=0,
            payload=struct.pack('>L', 1),
        )
        result = engine.handle_datagram(_encode(request), endpoint)
        self.assertFalse(result.errors)
        self.assertEqual(len(result.messages), 1)
        payload = result.messages[0].payload
        room_count = struct.unpack_from('>L', payload, 8)[0]
        self.assertEqual(room_count, 0)

    def test_duplicate_or_older_sequence_is_tolerated_for_authenticated_session(self) -> None:
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
        self.assertEqual(len(duplicate_result.messages), 1)

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
        self.assertEqual(len(older_result.messages), 1)

    def test_duplicate_reliable_send_is_ack_only(self) -> None:
        config = self._config
        engine = SnapProtocolEngine(config=config, plugin=AutoModellistaPlugin())
        endpoint_one = Endpoint(host='127.0.0.1', port=50100)
        endpoint_two = Endpoint(host='127.0.0.2', port=50101)

        sender_session = _create_session_via_login(engine, endpoint_one, 'test')
        receiver_session = _create_session_via_login(engine, endpoint_two, 'test')
        _join_lobby(engine, endpoint_one, sender_session, lobby_id=1, sequence=3)
        _join_lobby(engine, endpoint_two, receiver_session, lobby_id=1, sequence=3)

        room_id = _create_room(engine, endpoint_one, sender_session, sequence=4, room_name='dup-chat-room')
        _join_room(engine, endpoint_two, receiver_session, room_id=room_id, sequence=4)

        payload = struct.pack('>2B5s4s3s', 5, 4, b'test\n', b'team', b'aaa')
        request = SnapMessage(
            endpoint=endpoint_one,
            type_flags=0xA400,
            packet_number=0,
            command=commands.CMD_SEND,
            session_id=sender_session,
            sequence_number=22,
            acknowledge_number=0,
            payload=payload,
        )

        first_result = engine.handle_datagram(_encode(request), endpoint_one)
        self.assertFalse(first_result.errors)
        self.assertEqual(len(first_result.messages), 2)
        self.assertEqual(first_result.messages[0].command, commands.CMD_ACK)
        self.assertEqual(first_result.messages[0].endpoint, endpoint_one)
        self.assertEqual(first_result.messages[1].command, commands.CMD_SEND)
        self.assertEqual(first_result.messages[1].endpoint, endpoint_two)

        duplicate_result = engine.handle_datagram(_encode(request), endpoint_one)
        self.assertFalse(duplicate_result.errors)
        self.assertEqual(len(duplicate_result.messages), 1)
        self.assertEqual(duplicate_result.messages[0].command, commands.CMD_ACK)
        self.assertEqual(duplicate_result.messages[0].endpoint, endpoint_one)

    def test_duplicate_reliable_leave_is_ack_only(self) -> None:
        config = self._config
        engine = SnapProtocolEngine(config=config, plugin=AutoModellistaPlugin())
        endpoint = Endpoint(host='127.0.0.1', port=50140)

        session_id = _create_session_via_login(engine, endpoint, 'test')
        _join_lobby(engine, endpoint, session_id, lobby_id=1, sequence=3)
        _create_room(engine, endpoint, session_id, sequence=4, room_name='dup-leave-room')

        request = SnapMessage(
            endpoint=endpoint,
            type_flags=CHANNEL_ROOM | FLAG_RELIABLE,
            packet_number=0,
            command=commands.CMD_LEAVE,
            session_id=session_id,
            sequence_number=22,
            acknowledge_number=0,
            payload=b'',
        )

        first_result = engine.handle_datagram(_encode(request), endpoint)
        self.assertFalse(first_result.errors)
        self.assertEqual(len(first_result.messages), 1)
        self.assertEqual(first_result.messages[0].command, commands.CMD_RESULT_WRAPPER)

        duplicate_result = engine.handle_datagram(_encode(request), endpoint)
        self.assertFalse(duplicate_result.errors)
        self.assertEqual(len(duplicate_result.messages), 1)
        self.assertEqual(duplicate_result.messages[0].command, commands.CMD_ACK)
        self.assertEqual(duplicate_result.messages[0].endpoint, endpoint)
        self.assertEqual(duplicate_result.messages[0].acknowledge_number, 22)

    def test_duplicate_reliable_lobby_leave_is_ack_only(self) -> None:
        config = self._config
        engine = SnapProtocolEngine(config=config, plugin=AutoModellistaPlugin())
        endpoint = Endpoint(host='127.0.0.1', port=50141)

        session_id = _create_session_via_login(engine, endpoint, 'test')
        _join_lobby(engine, endpoint, session_id, lobby_id=1, sequence=3)

        request = SnapMessage(
            endpoint=endpoint,
            type_flags=CHANNEL_LOBBY | FLAG_RELIABLE,
            packet_number=0,
            command=commands.CMD_LEAVE,
            session_id=session_id,
            sequence_number=23,
            acknowledge_number=0,
            payload=b'',
        )

        first_result = engine.handle_datagram(_encode(request), endpoint)
        self.assertFalse(first_result.errors)
        self.assertEqual(len(first_result.messages), 1)
        self.assertEqual(first_result.messages[0].command, commands.CMD_RESULT_WRAPPER)

        duplicate_result = engine.handle_datagram(_encode(request), endpoint)
        self.assertFalse(duplicate_result.errors)
        self.assertEqual(len(duplicate_result.messages), 1)
        self.assertEqual(duplicate_result.messages[0].command, commands.CMD_ACK)
        self.assertEqual(duplicate_result.messages[0].endpoint, endpoint)
        self.assertEqual(duplicate_result.messages[0].acknowledge_number, 23)

    def test_duplicate_reliable_send_target_is_ack_only(self) -> None:
        config = self._config
        engine = SnapProtocolEngine(config=config, plugin=AutoModellistaPlugin())
        endpoint_one = Endpoint(host='127.0.0.1', port=50102)
        endpoint_two = Endpoint(host='127.0.0.2', port=50103)

        sender_session = _create_session_via_login(engine, endpoint_one, 'test')
        receiver_session = _create_session_via_login(engine, endpoint_two, 'test')
        _join_lobby(engine, endpoint_one, sender_session, lobby_id=1, sequence=3)
        _join_lobby(engine, endpoint_two, receiver_session, lobby_id=1, sequence=3)

        room_id = _create_room(engine, endpoint_one, sender_session, sequence=4, room_name='dup-target-room')
        _join_room(engine, endpoint_two, receiver_session, room_id=room_id, sequence=4)

        request = SnapMessage(
            endpoint=endpoint_one,
            type_flags=CHANNEL_ROOM | FLAG_RELIABLE,
            packet_number=0,
            command=commands.CMD_SEND_TARGET,
            session_id=sender_session,
            sequence_number=5,
            acknowledge_number=0,
            payload=struct.pack('>2LH', 1, receiver_session, 0x8103),
        )

        first_result = engine.handle_datagram(_encode(request), endpoint_one)
        self.assertFalse(first_result.errors)
        self.assertEqual(len(first_result.messages), 2)
        self.assertEqual(first_result.messages[0].command, commands.CMD_ACK)
        self.assertEqual(first_result.messages[1].command, commands.CMD_SEND_TARGET)
        self.assertEqual(first_result.messages[1].endpoint, endpoint_two)

        duplicate_result = engine.handle_datagram(_encode(request), endpoint_one)
        self.assertFalse(duplicate_result.errors)
        self.assertEqual(len(duplicate_result.messages), 1)
        self.assertEqual(duplicate_result.messages[0].command, commands.CMD_ACK)
        self.assertEqual(duplicate_result.messages[0].endpoint, endpoint_one)
        self.assertEqual(duplicate_result.messages[0].acknowledge_number, 5)

    def test_duplicate_reliable_send_response_flag_is_ack_only(self) -> None:
        config = self._config
        engine = SnapProtocolEngine(config=config, plugin=AutoModellistaPlugin())
        endpoint_one = Endpoint(host='127.0.0.1', port=50104)
        endpoint_two = Endpoint(host='127.0.0.2', port=50105)

        sender_session = _create_session_via_login(engine, endpoint_one, 'test')
        receiver_session = _create_session_via_login(engine, endpoint_two, 'test')
        _join_lobby(engine, endpoint_one, sender_session, lobby_id=1, sequence=3)
        _join_lobby(engine, endpoint_two, receiver_session, lobby_id=1, sequence=3)

        room_id = _create_room(engine, endpoint_one, sender_session, sequence=4, room_name='dup-game-room')
        _join_room(engine, endpoint_two, receiver_session, room_id=room_id, sequence=4)

        request = SnapMessage(
            endpoint=endpoint_one,
            type_flags=0xE000,
            packet_number=0,
            command=commands.CMD_SEND,
            session_id=sender_session,
            sequence_number=1482,
            acknowledge_number=1619,
            payload=bytes.fromhex(
                '4020000029f44027e8147f3f68d78e3d'
                '17514c42d025dd448a9085be8ed20341'
                '00000000050005008d7ca93eca326140'
                '2dea384030fc0880f0ce71bdf73012bf'
            ),
        )

        first_result = engine.handle_datagram(_encode(request), endpoint_one)
        self.assertFalse(first_result.errors)
        self.assertEqual(len(first_result.messages), 2)
        self.assertEqual(first_result.messages[0].command, commands.CMD_ACK)
        self.assertEqual(first_result.messages[0].endpoint, endpoint_one)
        self.assertEqual(first_result.messages[1].command, commands.CMD_SEND)
        self.assertEqual(first_result.messages[1].endpoint, endpoint_two)

        duplicate = SnapMessage(
            endpoint=endpoint_one,
            type_flags=0xE000,
            packet_number=0,
            command=commands.CMD_SEND,
            session_id=sender_session,
            sequence_number=1482,
            acknowledge_number=1621,
            payload=request.payload,
        )

        duplicate_result = engine.handle_datagram(_encode(duplicate), endpoint_one)
        self.assertFalse(duplicate_result.errors)
        self.assertEqual(len(duplicate_result.messages), 1)
        self.assertEqual(duplicate_result.messages[0].command, commands.CMD_ACK)
        self.assertEqual(duplicate_result.messages[0].endpoint, endpoint_one)

    def test_duplicate_reliable_send_replays_when_sequence_guard_is_bypassed(self) -> None:
        config = self._config
        engine = SnapProtocolEngine(config=config, plugin=AutoModellistaPlugin())
        endpoint_one = Endpoint(host='127.0.0.1', port=50106)
        endpoint_two = Endpoint(host='127.0.0.2', port=50107)

        sender_session = _create_session_via_login(engine, endpoint_one, 'test')
        receiver_session = _create_session_via_login(engine, endpoint_two, 'test')
        _join_lobby(engine, endpoint_one, sender_session, lobby_id=1, sequence=3)
        _join_lobby(engine, endpoint_two, receiver_session, lobby_id=1, sequence=3)

        room_id = _create_room(engine, endpoint_one, sender_session, sequence=4, room_name='dup-cache-room')
        _join_room(engine, endpoint_two, receiver_session, room_id=room_id, sequence=4)

        request = SnapMessage(
            endpoint=endpoint_one,
            type_flags=0xA400,
            packet_number=0,
            command=commands.CMD_SEND,
            session_id=sender_session,
            sequence_number=22,
            acknowledge_number=0,
            payload=struct.pack('>2B5s4s3s', 5, 4, b'test\n', b'team', b'aaa'),
        )

        first_result = engine.handle_datagram(_encode(request), endpoint_one)
        self.assertFalse(first_result.errors)
        self.assertEqual(len(first_result.messages), 2)
        self.assertEqual(first_result.messages[0].command, commands.CMD_ACK)
        self.assertEqual(first_result.messages[0].endpoint, endpoint_one)
        self.assertEqual(first_result.messages[1].command, commands.CMD_SEND)
        self.assertEqual(first_result.messages[1].endpoint, endpoint_two)

        with patch.object(engine._sessions, 'accept_incoming', return_value=True):  # noqa: SLF001
            duplicate_result = engine.handle_datagram(_encode(request), endpoint_one)
        self.assertFalse(duplicate_result.errors)
        self.assertEqual(len(duplicate_result.messages), 2)
        self.assertEqual(duplicate_result.messages[0].command, commands.CMD_ACK)
        self.assertEqual(duplicate_result.messages[0].endpoint, endpoint_one)
        self.assertEqual(duplicate_result.messages[1].command, commands.CMD_SEND)
        self.assertEqual(duplicate_result.messages[1].endpoint, endpoint_two)

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


def _encode_many(messages: list[SnapMessage]) -> bytes:
    """Encode multiple request messages in one datagram."""

    from opensnap.protocol.codec import encode_messages

    return encode_messages(messages)


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


def _decrypt_blowfish(key: bytes, payload: bytes) -> bytes:
    """Decrypt test payload using Blowfish ECB."""

    cipher = Cipher(decrepit_algorithms.Blowfish(key), modes.ECB(), backend=default_backend())
    decryptor = cipher.decryptor()
    return decryptor.update(payload) + decryptor.finalize()


def _pad_block(payload: bytes, block_size: int) -> bytes:
    """Pad payload with null bytes."""

    missing = (-len(payload)) % block_size
    if missing == 0:
        return payload
    return payload + (b'\x00' * missing)


if __name__ == '__main__':
    unittest.main()
