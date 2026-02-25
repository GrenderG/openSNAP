"""SQLite backend integration tests."""

from dataclasses import replace
import sqlite3
import tempfile
import unittest

from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.decrepit.ciphers import algorithms as decrepit_algorithms
from cryptography.hazmat.primitives.ciphers import Cipher, modes

from opensnap.config import StorageConfig, default_app_config
from opensnap.core.engine import SnapProtocolEngine
from opensnap.plugins.automodellista import AutoModellistaPlugin
from opensnap.protocol import commands
from opensnap.protocol.constants import CHANNEL_LOBBY
from opensnap.protocol.models import Endpoint, SnapMessage


class SqliteBackendTests(unittest.TestCase):
    """Engine tests using SQLite storage."""

    def test_sqlite_login_flow_and_team_persistence(self) -> None:
        with tempfile.TemporaryDirectory() as temp_directory:
            database_path = f'{temp_directory}/opensnap.sqlite'
            config = replace(
                default_app_config(),
                storage=StorageConfig(backend='sqlite', sqlite_path=database_path),
            )
            engine = SnapProtocolEngine(config=config, plugin=AutoModellistaPlugin())
            endpoint = Endpoint(host='127.0.0.1', port=50100)

            login_result = engine.handle_datagram(
                _encode(
                    SnapMessage(
                        endpoint=endpoint,
                        type_flags=CHANNEL_LOBBY,
                        packet_number=0,
                        command=commands.CMD_LOGIN_CLIENT,
                        session_id=0,
                        sequence_number=0,
                        acknowledge_number=0,
                        payload=b'test\n\x00',
                    )
                ),
                endpoint,
            )
            self.assertFalse(login_result.errors)
            self.assertEqual(login_result.messages[0].command, commands.CMD_BOOTSTRAP_LOGIN_SWAN)

            session_id = login_result.messages[0].session_id
            check_result = engine.handle_datagram(
                _encode(
                    SnapMessage(
                        endpoint=endpoint,
                        type_flags=CHANNEL_LOBBY,
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
                ),
                endpoint,
            )
            self.assertFalse(check_result.errors)
            self.assertEqual(check_result.messages[0].command, commands.CMD_BOOTSTRAP_LOGIN_SUCCESS)

            team_payload = bytearray(0x130)
            team_payload[0x128:0x12D] = b'sql\x00'
            kics_result = engine.handle_datagram(
                _encode(
                    SnapMessage(
                        endpoint=endpoint,
                        type_flags=CHANNEL_LOBBY,
                        packet_number=0,
                        command=commands.CMD_LOGIN_TO_KICS,
                        session_id=session_id,
                        sequence_number=2,
                        acknowledge_number=0,
                        payload=bytes(team_payload),
                    )
                ),
                endpoint,
            )
            self.assertFalse(kics_result.errors)
            self.assertEqual(kics_result.messages[0].command, 0x29)

            with sqlite3.connect(database_path) as connection:
                row = connection.execute(
                    'SELECT team, password FROM users WHERE username = ?',
                    ('test',),
                ).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row[0], 'sql')
            self.assertTrue(str(row[1]).startswith('v1$'))
            self.assertNotEqual(row[1], '1111')

    def test_sqlite_seed_is_stable_for_lobbies(self) -> None:
        with tempfile.TemporaryDirectory() as temp_directory:
            database_path = f'{temp_directory}/opensnap.sqlite'
            config = replace(
                default_app_config(),
                storage=StorageConfig(backend='sqlite', sqlite_path=database_path),
            )
            SnapProtocolEngine(config=config, plugin=AutoModellistaPlugin())
            SnapProtocolEngine(config=config, plugin=AutoModellistaPlugin())

            with sqlite3.connect(database_path) as connection:
                row = connection.execute('SELECT COUNT(*) FROM lobbies').fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row[0], len(config.lobbies))

    def test_sqlite_generates_non_empty_per_account_seeds(self) -> None:
        with tempfile.TemporaryDirectory() as temp_directory:
            database_path = f'{temp_directory}/opensnap.sqlite'
            config = replace(
                default_app_config(),
                storage=StorageConfig(backend='sqlite', sqlite_path=database_path),
            )
            SnapProtocolEngine(config=config, plugin=AutoModellistaPlugin())

            with sqlite3.connect(database_path) as connection:
                rows = connection.execute('SELECT seed FROM users ORDER BY user_id').fetchall()

            self.assertEqual(len(rows), len(config.users))
            seeds = [str(row[0]) for row in rows]
            self.assertTrue(all(seed for seed in seeds))
            if len(seeds) > 1:
                self.assertGreater(len(set(seeds)), 1)


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
