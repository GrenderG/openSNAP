"""Engine integration tests."""

import struct
import unittest

from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.decrepit.ciphers import algorithms as decrepit_algorithms
from cryptography.hazmat.primitives.ciphers import Cipher, modes

from opensnap.config import default_app_config
from opensnap.core.engine import SnapProtocolEngine
from opensnap.plugins.automodellista import AutoModellistaPlugin
from opensnap.protocol import commands
from opensnap.protocol.constants import CHANNEL_LOBBY
from opensnap.protocol.fields import get_u32
from opensnap.protocol.models import Endpoint, SnapMessage


class EngineFlowTests(unittest.TestCase):
    """Smoke tests for main login and lobby flow."""

    def test_login_and_kics_flow(self) -> None:
        config = default_app_config()
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
        config = default_app_config()
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


def _encode(message: SnapMessage) -> bytes:
    """Encode request message as datagram."""

    from opensnap.protocol.codec import encode_messages

    return encode_messages([message])


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
