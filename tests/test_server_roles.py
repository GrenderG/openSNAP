"""Bootstrap and game server role tests."""

from dataclasses import replace
import tempfile
import unittest

from opensnap.config import GameServerTargetConfig, StorageConfig, default_app_config
from opensnap.core.bootstrap import _encrypt_blowfish_ecb
from opensnap.core.engine import SnapProtocolEngine
from opensnap.plugins.automodellista import AutoModellistaPlugin
from opensnap.protocol import commands
from opensnap.protocol.codec import encode_messages
from opensnap.protocol.constants import FLAG_CHANNEL_BITS
from opensnap.protocol.fields import get_u32
from opensnap.protocol.models import Endpoint, SnapMessage


class ServerRoleTests(unittest.TestCase):
    """Verify the separated bootstrap and game engine roles."""

    def setUp(self) -> None:
        self._temp_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp_directory.cleanup)
        self._config = replace(
            default_app_config(),
            storage=StorageConfig(
                backend='sqlite',
                sqlite_path=f'{self._temp_directory.name}/server-roles.sqlite',
            ),
        )

    def test_bootstrap_session_survives_game_server_startup_and_rebinds_endpoint(self) -> None:
        bootstrap_engine = SnapProtocolEngine(config=self._config, role='bootstrap')
        bootstrap_endpoint = Endpoint(host='127.0.0.1', port=50010)

        login_request = SnapMessage(
            endpoint=bootstrap_endpoint,
            type_flags=FLAG_CHANNEL_BITS,
            packet_number=0,
            command=commands.CMD_LOGIN_CLIENT,
            session_id=0,
            sequence_number=0,
            acknowledge_number=0,
            payload=b'test\n\x00',
        )
        login_result = bootstrap_engine.handle_datagram(_encode(login_request), bootstrap_endpoint)
        self.assertFalse(login_result.errors)
        self.assertEqual(len(login_result.messages), 1)

        session_id = login_result.messages[0].session_id
        check_request = SnapMessage(
            endpoint=bootstrap_endpoint,
            type_flags=FLAG_CHANNEL_BITS,
            packet_number=0,
            command=commands.CMD_BOOTSTRAP_LOGIN_SWAN_CHECK,
            session_id=session_id,
            sequence_number=1,
            acknowledge_number=0,
            payload=_build_valid_bootstrap_check_payload(
                self._config.server.bootstrap_key,
                self._config.server.server_secret,
            ),
        )
        check_result = bootstrap_engine.handle_datagram(_encode(check_request), bootstrap_endpoint)
        self.assertFalse(check_result.errors)
        self.assertEqual(check_result.messages[0].command, commands.CMD_BOOTSTRAP_LOGIN_SUCCESS)

        game_engine = SnapProtocolEngine(
            config=self._config,
            plugin=AutoModellistaPlugin(),
            role='game',
        )
        game_endpoint = Endpoint(host='127.0.0.1', port=50011)
        team_payload = bytearray(0x130)
        team_payload[0x128:0x12F] = b'team-a\x00'
        kics_request = SnapMessage(
            endpoint=game_endpoint,
            type_flags=FLAG_CHANNEL_BITS,
            packet_number=0,
            command=commands.CMD_LOGIN_TO_KICS,
            session_id=session_id,
            sequence_number=2,
            acknowledge_number=0,
            payload=bytes(team_payload),
        )
        kics_result = game_engine.handle_datagram(_encode(kics_request), game_endpoint)

        self.assertFalse(kics_result.errors)
        self.assertEqual(len(kics_result.messages), 1)
        self.assertEqual(kics_result.messages[0].command, commands.CMD_RESULT_LOGIN_TO_KICS)
        rebound_session = game_engine._sessions.get(session_id)
        assert rebound_session is not None
        self.assertEqual(rebound_session.endpoint, game_endpoint)

    def test_bootstrap_role_rejects_game_only_login_to_kics_handler(self) -> None:
        engine = SnapProtocolEngine(config=self._config, role='bootstrap')
        endpoint = Endpoint(host='127.0.0.1', port=50012)
        request = SnapMessage(
            endpoint=endpoint,
            type_flags=FLAG_CHANNEL_BITS,
            packet_number=0,
            command=commands.CMD_LOGIN_TO_KICS,
            session_id=0x11223344,
            sequence_number=0,
            acknowledge_number=0,
            payload=b'',
        )

        result = engine.handle_datagram(_encode(request), endpoint)

        self.assertFalse(result.messages)
        self.assertEqual(len(result.errors), 1)
        self.assertIn('Unhandled command 0x01', result.errors[0])

    def test_game_role_rejects_bootstrap_only_login_client_handler(self) -> None:
        engine = SnapProtocolEngine(
            config=self._config,
            plugin=AutoModellistaPlugin(),
            role='game',
        )
        endpoint = Endpoint(host='127.0.0.1', port=50013)
        request = SnapMessage(
            endpoint=endpoint,
            type_flags=FLAG_CHANNEL_BITS,
            packet_number=0,
            command=commands.CMD_LOGIN_CLIENT,
            session_id=0,
            sequence_number=0,
            acknowledge_number=0,
            payload=b'test\n\x00',
        )

        result = engine.handle_datagram(_encode(request), endpoint)

        self.assertFalse(result.messages)
        self.assertEqual(len(result.errors), 1)
        self.assertIn('Unhandled command 0x2c', result.errors[0])

    def test_bootstrap_success_payload_uses_explicit_game_target_mapping(self) -> None:
        config = replace(
            self._config,
            server=replace(
                self._config.server,
                default_bootstrap_game_identifier='monsterhunter',
                game_targets=(
                    GameServerTargetConfig(
                        game_identifier='monsterhunter',
                        host='203.0.113.90',
                        port=10090,
                    ),
                ),
            ),
        )
        engine = SnapProtocolEngine(config=config, role='bootstrap')
        endpoint = Endpoint(host='127.0.0.1', port=50014)

        login_request = SnapMessage(
            endpoint=endpoint,
            type_flags=FLAG_CHANNEL_BITS,
            packet_number=0,
            command=commands.CMD_LOGIN_CLIENT,
            session_id=0,
            sequence_number=0,
            acknowledge_number=0,
            payload=b'test\n\x00',
        )
        login_result = engine.handle_datagram(_encode(login_request), endpoint)
        self.assertFalse(login_result.errors)
        session_id = login_result.messages[0].session_id

        check_request = SnapMessage(
            endpoint=endpoint,
            type_flags=FLAG_CHANNEL_BITS,
            packet_number=0,
            command=commands.CMD_BOOTSTRAP_LOGIN_SWAN_CHECK,
            session_id=session_id,
            sequence_number=1,
            acknowledge_number=0,
            payload=_build_valid_bootstrap_check_payload(
                config.server.bootstrap_key,
                config.server.server_secret,
            ),
        )
        check_result = engine.handle_datagram(_encode(check_request), endpoint)

        self.assertFalse(check_result.errors)
        clear = _decrypt_blowfish(config.server.bootstrap_key, check_result.messages[0].payload)
        self.assertEqual(get_u32(clear, 40), 0xCB00715A)
        self.assertEqual(get_u32(clear, 44), 10090)
        self.assertEqual(get_u32(clear, 48), 10090)

    def test_bootstrap_login_fails_when_default_game_target_is_not_explicitly_mapped(self) -> None:
        config = replace(
            self._config,
            server=replace(
                self._config.server,
                default_bootstrap_game_identifier='monsterhunter',
                game_targets=(
                    GameServerTargetConfig(
                        game_identifier=self._config.server.game_identifier,
                        host=self._config.server.game.advertise_host or self._config.server.game.host,
                        port=self._config.server.game.port,
                    ),
                ),
            ),
        )
        engine = SnapProtocolEngine(config=config, role='bootstrap')
        endpoint = Endpoint(host='127.0.0.1', port=50015)
        request = SnapMessage(
            endpoint=endpoint,
            type_flags=FLAG_CHANNEL_BITS,
            packet_number=0,
            command=commands.CMD_LOGIN_CLIENT,
            session_id=0,
            sequence_number=0,
            acknowledge_number=0,
            payload=b'test\n\x00',
        )

        result = engine.handle_datagram(_encode(request), endpoint)

        self.assertFalse(result.errors)
        self.assertEqual(len(result.messages), 1)
        self.assertEqual(result.messages[0].command, commands.CMD_BOOTSTRAP_LOGIN_FAIL)


def _encode(message: SnapMessage) -> bytes:
    """Encode one request message for the engine."""

    return encode_messages([message])


def _build_valid_bootstrap_check_payload(bootstrap_key: bytes, server_secret: str) -> bytes:
    """Build the minimal bootstrap-check payload accepted by auth."""

    plaintext = bytearray(136)
    plaintext[8:8 + len(server_secret)] = server_secret.encode('utf-8')
    plaintext[8 + len(server_secret)] = 0
    return _encrypt_blowfish_ecb(bootstrap_key, bytes(plaintext))


def _decrypt_blowfish(bootstrap_key: bytes, payload: bytes) -> bytes:
    """Decrypt a bootstrap payload for assertions."""

    from cryptography.hazmat.backends import default_backend
    from cryptography.hazmat.decrepit.ciphers import algorithms as decrepit_algorithms
    from cryptography.hazmat.primitives.ciphers import Cipher, modes

    cipher = Cipher(decrepit_algorithms.Blowfish(bootstrap_key), modes.ECB(), backend=default_backend())
    decryptor = cipher.decryptor()
    return decryptor.update(payload) + decryptor.finalize()


if __name__ == '__main__':
    unittest.main()
