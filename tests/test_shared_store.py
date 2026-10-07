"""Shared store contract (accounts, session handoffs, records, online players) for every SQL backend.

MariaDB runs when `OPENSNAP_TEST_MARIADB_HOST` (plus `_PORT`, `_USER`,
`_PASSWORD`, `_DATABASE`) points at a disposable test database and PyMySQL is
installed, and PostgreSQL when `OPENSNAP_TEST_POSTGRESQL_HOST` (same suffixes)
does and psycopg is installed; otherwise they are skipped.
"""

from dataclasses import replace
import importlib.util
import os
import tempfile
import unittest

from opensnap.config import StorageConfig, UserConfig, default_app_config
from opensnap.core.accounts import verify_password
from opensnap.core.sessions import Session
from opensnap.protocol.models import Endpoint
from opensnap.storage.factory import create_storage, open_connection
from opensnap.storage.interfaces import DuplicateAccountError
from opensnap.storage.sql import seed_users

MARIADB_HOST = os.getenv('OPENSNAP_TEST_MARIADB_HOST', '')
POSTGRESQL_HOST = os.getenv('OPENSNAP_TEST_POSTGRESQL_HOST', '')


class SharedStoreContract:
    """Behavior every shared-store backend must provide."""

    def storage_config(self) -> StorageConfig:
        raise NotImplementedError

    def setUp(self) -> None:
        self.config = replace(
            default_app_config(),
            storage=self.storage_config(),
            users=(UserConfig(user_id=1, username='test', password='1111'),),
        )
        self.storage = create_storage(self.config)
        self.addCleanup(self.storage.close)

    def test_accounts_are_created_seeded_and_updated(self) -> None:
        accounts = self.storage.accounts
        seeded = accounts.get_by_name('test')
        self.assertIsNotNone(seeded)
        self.assertTrue(verify_password(seeded, '1111'))

        created = accounts.create_user('alice', 'secret')
        self.assertEqual(accounts.get_by_id(created.user_id).username, 'alice')
        with self.assertRaises(DuplicateAccountError):
            accounts.create_user('alice', 'other')

        accounts.set_team(created.user_id, 'team-a')
        self.assertEqual(accounts.get_by_name('alice').team, 'team-a')

        # Seeding again (every service does at startup) keeps existing rows.
        connection = open_connection(self.config)
        self.addCleanup(connection.close)
        seed_users(connection, self.config.users)
        self.assertEqual(accounts.get_by_name('test').user_id, seeded.user_id)

    def test_handoffs_are_issued_replaced_and_removed(self) -> None:
        handoffs = self.storage.handoffs
        account = self.storage.accounts.get_by_name('test')
        endpoint = Endpoint(host='10.0.0.1', port=2000)

        first = handoffs.issue(endpoint, account, game_identifier='automodellista')
        handoffs.set_sequence(first.session_id, 2)
        stored = handoffs.get(first.session_id)
        self.assertEqual((stored.serial, stored.sequence_number, stored.game_identifier), (first.serial, 2, 'automodellista'))
        self.assertEqual(handoffs.get_by_endpoint(endpoint), stored)

        # A new login replaces the handoff and gets a new serial.
        second = handoffs.issue(endpoint, account, game_identifier='monsterhunter')
        self.assertEqual(second.session_id, first.session_id)
        self.assertGreater(second.serial, first.serial)
        self.assertEqual(handoffs.get(first.session_id).sequence_number, 0)

        # A login from another endpoint of the same client host keeps one row per session id.
        moved = handoffs.issue(Endpoint(host='10.0.0.1', port=2001), account, game_identifier='monsterhunter')
        self.assertIsNone(handoffs.get_by_endpoint(endpoint))
        self.assertEqual(handoffs.get(moved.session_id).endpoint.port, 2001)

        handoffs.remove(moved.session_id)
        self.assertIsNone(handoffs.get(moved.session_id))

    def test_online_players_keep_login_order_areas_and_games_apart(self) -> None:
        online = self.storage.online_players
        for game in ('monsterhunter', 'outbreak'):
            online.clear(game)

        def login(game: str, session_id: int) -> None:
            endpoint = Endpoint(host='10.0.0.1', port=4000)
            online.login(game, Session(session_id=session_id, user_id=1, username='test', endpoint=endpoint))

        login('monsterhunter', 2)
        login('monsterhunter', 1)
        login('outbreak', 3)
        online.set_area(1, 5)
        first = online.list('monsterhunter')
        self.assertEqual([(player.session_id, player.area_id) for player in first], [(2, 0), (1, 5)])

        # A repeated login keeps the row and its Area, with a new, later serial.
        login('monsterhunter', 2)
        again = online.list('monsterhunter')
        self.assertEqual([(player.session_id, player.area_id) for player in again], [(1, 5), (2, 0)])
        self.assertGreater(again[1].login_serial, first[0].login_serial)

        online.logout(1)
        self.assertEqual([player.session_id for player in online.list('monsterhunter')], [2])
        online.clear('monsterhunter')
        self.assertEqual(online.list('monsterhunter'), [])
        self.assertEqual([player.session_id for player in online.list('outbreak')], [3])
        online.clear('outbreak')

    def test_records_rank_each_players_best_per_board(self) -> None:
        records = self.storage.records
        records.add(game='am', board='A', user_id=1, player='test', score=900, details={'team': 'x'})
        records.add(game='am', board='A', user_id=2, player='alice', score=800, details={})
        records.add(game='am', board='A', user_id=1, player='test', score=700, details={'team': 'y'})
        records.add(game='am', board='A', user_id=1, player='test', score=700, details={'team': 'z'})
        records.add(game='am', board='B', user_id=2, player='alice', score=5, details={})
        records.add(game='other', board='A', user_id=3, player='bob', score=1, details={})

        boards = records.best_by_board('am', 10)
        self.assertEqual(
            [(record.player, record.score, dict(record.details)) for record in boards['A']],
            # A repeated best keeps its first submission.
            [('test', 700, {'team': 'y'}), ('alice', 800, {})],
        )
        self.assertEqual([record.player for record in boards['B']], ['alice'])
        self.assertEqual(set(boards), {'A', 'B'})
        self.assertEqual(len(records.best_by_board('am', 1)['A']), 1)

    def test_records_total_per_player_over_board_prefix(self) -> None:
        records = self.storage.records
        records.add(game='mh', board='hunts-1', user_id=1, player='a', score=3, details={})
        records.add(game='mh', board='hunts-2', user_id=1, player='a', score=4, details={})
        records.add(game='mh', board='hunts-1', user_id=2, player='b', score=5, details={})
        records.add(game='mh', board='clear-1', user_id=2, player='b', score=999, details={})
        records.add(game='am', board='hunts-1', user_id=3, player='c', score=100, details={})
        self.assertEqual(
            [(total.player, total.total) for total in records.totals('mh', 'hunts-', 10)],
            [('a', 7), ('b', 5)],
        )
        self.assertEqual(len(records.totals('mh', 'hunts-', 1)), 1)


class SqliteSharedStoreTests(SharedStoreContract, unittest.TestCase):
    def storage_config(self) -> StorageConfig:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        return StorageConfig(backend='sqlite', sqlite_path=f'{directory.name}/shared.sqlite')


@unittest.skipUnless(
    MARIADB_HOST and importlib.util.find_spec('pymysql') is not None,
    'MariaDB test server not configured (OPENSNAP_TEST_MARIADB_HOST) or PyMySQL missing.',
)
class MariaDbSharedStoreTests(SharedStoreContract, unittest.TestCase):
    def storage_config(self) -> StorageConfig:
        return StorageConfig(
            backend='mariadb',
            mariadb_host=MARIADB_HOST,
            mariadb_port=int(os.getenv('OPENSNAP_TEST_MARIADB_PORT', '3306')),
            mariadb_user=os.getenv('OPENSNAP_TEST_MARIADB_USER', 'root'),
            mariadb_password=os.getenv('OPENSNAP_TEST_MARIADB_PASSWORD', ''),
            mariadb_database=os.getenv('OPENSNAP_TEST_MARIADB_DATABASE', 'opensnap_test'),
        )

    def setUp(self) -> None:
        super().setUp()
        connection = open_connection(self.config)
        self.addCleanup(connection.close)
        connection.execute('DELETE FROM session_handoffs')
        connection.execute('DELETE FROM records')
        connection.execute("DELETE FROM users WHERE username <> 'test'")



@unittest.skipUnless(
    POSTGRESQL_HOST and importlib.util.find_spec('psycopg') is not None,
    'PostgreSQL test server not configured (OPENSNAP_TEST_POSTGRESQL_HOST) or psycopg missing.',
)
class PostgresqlSharedStoreTests(SharedStoreContract, unittest.TestCase):
    def storage_config(self) -> StorageConfig:
        return StorageConfig(
            backend='postgresql',
            postgresql_host=POSTGRESQL_HOST,
            postgresql_port=int(os.getenv('OPENSNAP_TEST_POSTGRESQL_PORT', '5432')),
            postgresql_user=os.getenv('OPENSNAP_TEST_POSTGRESQL_USER', 'postgres'),
            postgresql_password=os.getenv('OPENSNAP_TEST_POSTGRESQL_PASSWORD', ''),
            postgresql_database=os.getenv('OPENSNAP_TEST_POSTGRESQL_DATABASE', 'opensnap_test'),
        )

    def setUp(self) -> None:
        super().setUp()
        connection = open_connection(self.config)
        self.addCleanup(connection.close)
        connection.execute('DELETE FROM session_handoffs')
        connection.execute('DELETE FROM records')
        connection.execute("DELETE FROM users WHERE username <> 'test'")

if __name__ == '__main__':
    unittest.main()
