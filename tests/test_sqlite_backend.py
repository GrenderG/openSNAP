"""SQLite backend integration tests."""

from dataclasses import replace
import sqlite3
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
from opensnap.protocol.constants import FLAG_CHANNEL_BITS, FLAG_RELIABLE
from opensnap.protocol.models import Endpoint, SnapMessage
from tests.support import create_accounts, login_client_payload, serving


class SqliteBackendTests(unittest.TestCase):
    """Engine tests using SQLite storage."""

    def test_sqlite_login_flow_and_team_persistence(self) -> None:
        with tempfile.TemporaryDirectory() as temp_directory:
            database_path = f'{temp_directory}/opensnap.sqlite'
            config = replace(
                serving(default_app_config(), 'automodellista'),
                storage=StorageConfig(backend='sqlite', sqlite_path=database_path),
            )
            create_accounts(config, 'test')
            engine = SnapProtocolEngine(config=config, plugin=AutoModellistaPlugin())
            endpoint = Endpoint(host='127.0.0.1', port=50100)

            login_result = engine.handle_datagram(
                _encode(
                    SnapMessage(
                        endpoint=endpoint,
                        type_flags=FLAG_CHANNEL_BITS,
                        packet_number=0,
                        command=commands.CMD_LOGIN_CLIENT,
                        session_id=0,
                        sequence_number=0,
                        acknowledge_number=0,
                        payload=login_client_payload(b'test'),
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
                        type_flags=FLAG_CHANNEL_BITS,
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

    def test_legacy_runtime_tables_are_dropped(self) -> None:
        with tempfile.TemporaryDirectory() as temp_directory:
            database_path = f'{temp_directory}/opensnap.sqlite'
            with sqlite3.connect(database_path) as connection:
                for table in ('lobbies', 'sessions', 'rooms', 'room_members'):
                    connection.execute(f'CREATE TABLE {table} (id INTEGER)')
            config = replace(
                serving(default_app_config(), 'automodellista'),
                storage=StorageConfig(backend='sqlite', sqlite_path=database_path),
            )
            SnapProtocolEngine(config=config, plugin=AutoModellistaPlugin()).close()

            with sqlite3.connect(database_path) as connection:
                tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            self.assertEqual(tables - {'sqlite_sequence'}, {'users', 'session_handoffs', 'records', 'online_players'})

    def test_sqlite_generates_non_empty_per_account_seeds(self) -> None:
        with tempfile.TemporaryDirectory() as temp_directory:
            database_path = f'{temp_directory}/opensnap.sqlite'
            config = replace(
                serving(default_app_config(), 'automodellista'),
                storage=StorageConfig(backend='sqlite', sqlite_path=database_path),
            )
            create_accounts(config, 'alice', 'bob')

            with sqlite3.connect(database_path) as connection:
                rows = connection.execute('SELECT seed FROM users ORDER BY user_id').fetchall()

            seeds = [str(row[0]) for row in rows]
            self.assertEqual(len(seeds), 2)
            self.assertTrue(all(seeds))
            self.assertGreater(len(set(seeds)), 1)

    def test_split_bootstrap_and_game_servers_continue_unreliable_numbering(self) -> None:
        with tempfile.TemporaryDirectory() as temp_directory:
            config = replace(
                serving(default_app_config(), 'automodellista'),
                storage=StorageConfig(backend='sqlite', sqlite_path=f'{temp_directory}/opensnap.sqlite'),
            )
            create_accounts(config, 'test')
            # Separate processes (or machines) sharing only the shared store.
            bootstrap = SnapProtocolEngine(config=config, role='bootstrap')
            game = SnapProtocolEngine(config=config, plugin=AutoModellistaPlugin(), role='game')
            other_game_config = replace(config, server=replace(config.server, game_plugin='othergame'))
            other_game = SnapProtocolEngine(config=other_game_config, plugin=AutoModellistaPlugin(), role='game')
            for engine in (bootstrap, game, other_game):
                self.addCleanup(engine.close)
            endpoint = Endpoint(host='127.0.0.1', port=2000)

            session_id, success_sequence = _bootstrap_login(bootstrap, config, endpoint)
            self.assertEqual(success_sequence, 2)
            self.assertEqual(other_game.handle_datagram(_kics(endpoint, session_id), endpoint).messages, [])

            # The game server continues after the bootstrap's login success.
            first = game.handle_datagram(_kics(endpoint, session_id), endpoint).messages[0]
            self.assertEqual((first.command, first.sequence_number), (0x29, 3))
            # A repeated KICS of the same login keeps the counters.
            again = game.handle_datagram(_kics(endpoint, session_id), endpoint).messages[0]
            self.assertEqual(again.sequence_number, 4)

            # A new bootstrap login starts again from its own login success.
            session_id, _ = _bootstrap_login(bootstrap, config, endpoint)
            relogin = game.handle_datagram(_kics(endpoint, session_id), endpoint).messages[0]
            self.assertEqual(relogin.sequence_number, 3)

    def test_reliable_numbering_restarts_at_the_kics_login(self) -> None:
        # The client zeroes both reliable counters when it sends the KICS login
        # (AM `kkLoginToKICS` `0x2ee940..0x2ee950`), so the bootstrap's reliable
        # answer does not shift the game server's reliable numbering, whether
        # both run in one engine or in separate ones.
        for roles in (('bootstrap', 'game'), ('combined', 'combined')):
            with self.subTest(roles=roles), tempfile.TemporaryDirectory() as temp_directory:
                config = replace(
                    serving(default_app_config(), 'automodellista'),
                    storage=StorageConfig(backend='sqlite', sqlite_path=f'{temp_directory}/opensnap.sqlite'),
                )
                create_accounts(config, 'test')
                bootstrap = SnapProtocolEngine(config=config, plugin=AutoModellistaPlugin(), role=roles[0])
                game = bootstrap if roles[1] == 'combined' else SnapProtocolEngine(
                    config=config, plugin=AutoModellistaPlugin(), role=roles[1]
                )
                for engine in {bootstrap, game}:
                    self.addCleanup(engine.close)
                endpoint = Endpoint(host='127.0.0.1', port=2000)
                login = bootstrap.handle_datagram(
                    _encode(
                        SnapMessage(
                            endpoint=endpoint,
                            type_flags=FLAG_CHANNEL_BITS,
                            packet_number=0,
                            command=commands.CMD_LOGIN_CLIENT,
                            session_id=0,
                            sequence_number=0,
                            acknowledge_number=0,
                            payload=login_client_payload(b'test'),
                        )
                    ),
                    endpoint,
                ).messages[0]
                check = SnapMessage(
                    endpoint=endpoint,
                    type_flags=FLAG_CHANNEL_BITS | FLAG_RELIABLE,
                    packet_number=0,
                    command=commands.CMD_BOOTSTRAP_LOGIN_SWAN_CHECK,
                    session_id=login.session_id,
                    sequence_number=0,
                    acknowledge_number=0,
                    payload=_build_valid_bootstrap_check_payload(config.server.bootstrap_key, config.server.server_secret),
                )
                success = bootstrap.handle_datagram(_encode(check), endpoint).messages[0]
                self.assertEqual(success.command, commands.CMD_BOOTSTRAP_LOGIN_SUCCESS)
                self.assertEqual((success.type_flags & FLAG_RELIABLE, success.sequence_number), (FLAG_RELIABLE, 0))

                game.handle_datagram(_kics(endpoint, login.session_id), endpoint)
                join = SnapMessage(
                    endpoint=endpoint,
                    type_flags=FLAG_CHANNEL_BITS | FLAG_RELIABLE,
                    packet_number=0,
                    command=commands.CMD_JOIN,
                    session_id=login.session_id,
                    sequence_number=0,
                    acknowledge_number=0,
                    payload=struct.pack('>L', 1),
                )
                answers = game.handle_datagram(_encode(join), endpoint).messages

                self.assertEqual([answer.command for answer in answers], [commands.CMD_RESULT_WRAPPER])
                self.assertEqual((answers[0].type_flags & FLAG_RELIABLE, answers[0].sequence_number), (FLAG_RELIABLE, 0))

    def test_game_traffic_does_not_touch_the_shared_store(self) -> None:
        with tempfile.TemporaryDirectory() as temp_directory:
            config = replace(
                serving(default_app_config(), 'automodellista'),
                storage=StorageConfig(backend='sqlite', sqlite_path=f'{temp_directory}/opensnap.sqlite'),
            )
            create_accounts(config, 'test')
            engine = SnapProtocolEngine(config=config, plugin=AutoModellistaPlugin())
            self.addCleanup(engine.close)
            endpoint = Endpoint(host='127.0.0.1', port=2000)
            session_id, _ = _bootstrap_login(engine, config, endpoint)
            engine.handle_datagram(_kics(endpoint, session_id), endpoint)

            connection = engine._storage.handoffs._connection  # noqa: SLF001
            with (
                patch.object(connection, 'execute', wraps=connection.execute) as execute,
                patch.object(connection, 'query_one', wraps=connection.query_one) as query_one,
                patch.object(connection, 'query_all', wraps=connection.query_all) as query_all,
            ):
                for sequence in range(1, 21):
                    request = SnapMessage(
                        endpoint=endpoint,
                        type_flags=FLAG_CHANNEL_BITS | FLAG_RELIABLE,
                        packet_number=0,
                        command=commands.CMD_QUERY_LOBBIES,
                        session_id=session_id,
                        sequence_number=sequence,
                        acknowledge_number=0,
                        payload=b'',
                    )
                    self.assertTrue(engine.handle_datagram(_encode(request), endpoint).messages)
            self.assertEqual((execute.call_count, query_one.call_count, query_all.call_count), (0, 0, 0))


def _bootstrap_login(engine: SnapProtocolEngine, config, endpoint: Endpoint) -> tuple[int, int]:
    """Run login-client + SWAN check; return the session id and login-success sequence."""

    login = engine.handle_datagram(
        _encode(
            SnapMessage(
                endpoint=endpoint,
                type_flags=FLAG_CHANNEL_BITS,
                packet_number=0,
                command=commands.CMD_LOGIN_CLIENT,
                session_id=0,
                sequence_number=0,
                acknowledge_number=0,
                payload=login_client_payload(b'test'),
            )
        ),
        endpoint,
    ).messages[0]
    success = engine.handle_datagram(
        _encode(
            SnapMessage(
                endpoint=endpoint,
                type_flags=FLAG_CHANNEL_BITS,
                packet_number=0,
                command=commands.CMD_BOOTSTRAP_LOGIN_SWAN_CHECK,
                session_id=login.session_id,
                sequence_number=0,
                acknowledge_number=0,
                payload=_build_valid_bootstrap_check_payload(config.server.bootstrap_key, config.server.server_secret),
            )
        ),
        endpoint,
    ).messages[0]
    assert success.command == commands.CMD_BOOTSTRAP_LOGIN_SUCCESS
    return login.session_id, success.sequence_number


def _kics(endpoint: Endpoint, session_id: int) -> bytes:
    return _encode(
        SnapMessage(
            endpoint=endpoint,
            type_flags=FLAG_CHANNEL_BITS,
            packet_number=0,
            command=commands.CMD_LOGIN_TO_KICS,
            session_id=session_id,
            sequence_number=0,
            acknowledge_number=0,
            payload=bytes(0x130),
        )
    )


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
