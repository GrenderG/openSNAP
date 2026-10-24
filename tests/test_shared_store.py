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

from opensnap.config import StorageConfig, default_app_config
from opensnap.core.accounts import verify_password
from opensnap.core.records import Ranking
from opensnap.core.sessions import Session
from opensnap.protocol.models import Endpoint
from opensnap.storage.factory import create_storage, open_connection
from opensnap.storage.interfaces import DuplicateAccountError

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
        )
        self.storage = create_storage(self.config)
        self.addCleanup(self.storage.close)

    def test_accounts_are_created_and_updated(self) -> None:
        accounts = self.storage.accounts
        self.assertIsNone(accounts.get_by_name('alice'))

        created = accounts.create_user('alice', 'secret')
        self.assertTrue(verify_password(accounts.get_by_name('alice'), 'secret'))
        self.assertEqual(accounts.get_by_id(created.user_id).username, 'alice')
        with self.assertRaises(DuplicateAccountError):
            accounts.create_user('alice', 'other')

        accounts.set_team(created.user_id, 'team-a')
        self.assertEqual(accounts.get_by_name('alice').team, 'team-a')

    def test_recovery_code_sets_a_new_password_once(self) -> None:
        accounts = self.storage.accounts
        created = accounts.create_user('alice', 'secret')
        self.assertRegex(created.recovery_code, r'^[A-Z0-9]{12}$')
        self.assertEqual(accounts.get_by_name('alice').recovery_code, created.recovery_code)
        accounts.set_kage_login_key(created, b'secret')

        wrong = 'A' * 12 if created.recovery_code != 'A' * 12 else 'B' * 12
        self.assertIsNone(accounts.recover('alice', wrong, 'other'))
        self.assertIsNone(accounts.recover('nobody', created.recovery_code, 'other'))
        self.assertTrue(verify_password(accounts.get_by_name('alice'), 'secret'))

        recovered = accounts.recover('alice', created.recovery_code, 'other')
        self.assertNotEqual(recovered.recovery_code, created.recovery_code)
        self.assertEqual(recovered.seed, created.seed)
        self.assertEqual(recovered.kage_login_key, b'')
        stored = accounts.get_by_name('alice')
        self.assertTrue(verify_password(stored, 'other'))
        self.assertEqual(stored.recovery_code, recovered.recovery_code)
        # The old code is spent.
        self.assertIsNone(accounts.recover('alice', created.recovery_code, 'third'))

    def test_handoffs_are_issued_replaced_and_removed(self) -> None:
        handoffs = self.storage.handoffs
        account = self.storage.accounts.create_user('test', '1111')
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
        self.assertEqual(handoffs.get_by_login('10.0.0.1', 'test'), handoffs.get(moved.session_id))
        self.assertIsNone(handoffs.get_by_login('10.0.0.2', 'test'))

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

    def test_lowest_records_keep_each_players_best(self) -> None:
        records = self.storage.records
        lowest = Ranking.LOWEST
        records.submit(game='am', board='A', ranking=lowest, user_id=1, player='test', score=900, details={'team': 'x'})
        records.submit(game='am', board='A', ranking=lowest, user_id=2, player='alice', score=800)
        records.submit(game='am', board='A', ranking=lowest, user_id=1, player='test', score=700, details={'team': 'y'})
        # A repeated or worse score keeps the record and its details.
        records.submit(game='am', board='A', ranking=lowest, user_id=1, player='test', score=700, details={'team': 'z'})
        records.submit(game='am', board='A', ranking=lowest, user_id=1, player='test', score=950, details={'team': 'w'})
        records.submit(game='am', board='B', ranking=lowest, user_id=2, player='alice', score=5)
        records.submit(game='other', board='A', ranking=lowest, user_id=3, player='bob', score=1)

        self.assertEqual(
            [(record.player, record.score, dict(record.details)) for record in records.top('am', 'A', 10)],
            [('test', 700, {'team': 'y'}), ('alice', 800, {})],
        )
        self.assertEqual([record.player for record in records.top('am', 'A', 1)], ['test'])
        self.assertEqual(records.top('am', 'C', 10), [])
        self.assertEqual(records.boards('am'), ['A', 'B'])

    def test_highest_and_total_records_rank_highest_first(self) -> None:
        records = self.storage.records
        for user_id, score in ((1, 5), (2, 9), (1, 7), (2, 3)):
            records.submit(game='mh', board='best', ranking=Ranking.HIGHEST, user_id=user_id, player=f'p{user_id}',
                           score=score)
            records.submit(game='mh', board='sum', ranking=Ranking.TOTAL, user_id=user_id, player=f'p{user_id}',
                           score=score)
        self.assertEqual([(record.user_id, record.score) for record in records.top('mh', 'best', 10)], [(2, 9), (1, 7)])
        self.assertEqual([(record.user_id, record.score) for record in records.top('mh', 'sum', 10)],
                         [(1, 12), (2, 12)])
        # Adding nothing keeps when the total was reached.
        records.submit(game='mh', board='sum', ranking=Ranking.TOTAL, user_id=1, player='p1', score=0)
        self.assertEqual([record.user_id for record in records.top('mh', 'sum', 10)], [1, 2])

    def test_latest_records_keep_the_time_a_score_was_first_reached(self) -> None:
        records = self.storage.records

        def submit(user_id: int, score: int) -> None:
            records.submit(game='g', board='b', ranking=Ranking.LATEST, user_id=user_id, player=f'p{user_id}',
                           score=score)

        submit(1, 5)
        first = records.top('g', 'b', 1)[0]
        # The same score again keeps when it was reached; another score resets it.
        submit(1, 5)
        self.assertEqual(records.top('g', 'b', 1)[0].reached_at, first.reached_at)
        submit(2, 5)
        self.assertEqual([record.user_id for record in records.top('g', 'b', 10)], [1, 2])
        # A lower score replaces the record; reaching 5 again ranks after player 2.
        submit(1, 4)
        self.assertEqual([record.score for record in records.top('g', 'b', 10)], [5, 4])
        submit(1, 5)
        self.assertEqual([record.user_id for record in records.top('g', 'b', 10)], [2, 1])

    def test_entry_ranks_the_players_record(self) -> None:
        records = self.storage.records
        for user_id, score in ((1, 30), (2, 10), (3, 20), (4, 20)):
            records.submit(game='g', board='b', ranking=Ranking.LOWEST, user_id=user_id, player=f'p{user_id}',
                           score=score)
        self.assertEqual(
            [(entry.rank, entry.record.score) for entry in (records.entry('g', 'b', user) for user in (1, 2, 3, 4))],
            # Equal scores rank by who reached them first.
            [(4, 30), (1, 10), (2, 20), (3, 20)],
        )
        self.assertIsNone(records.entry('g', 'b', 5))
        self.assertIsNone(records.entry('g', 'other', 1))


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
