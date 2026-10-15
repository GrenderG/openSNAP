"""Signup service tests."""

import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from opensnap.config import default_app_config
from opensnap.storage.factory import create_storage
from opensnap_web.signup import SignupService


class SignupServiceTests(unittest.TestCase):
    """Verify create/login behavior backed by the shared store."""

    def test_create_then_login_then_wrong_password(self) -> None:
        with tempfile.TemporaryDirectory() as temp_directory:
            database_path = f'{temp_directory}/web-signup.sqlite'
            with patch.dict(
                os.environ,
                {'OPENSNAP_SQLITE_PATH': database_path},
                clear=True,
            ):
                storage = create_storage(default_app_config())
                self.addCleanup(storage.close)
                service = SignupService(storage.accounts)

                created = service.create_or_login(username='alice', password='pass123')
                self.assertTrue(created.ok)
                self.assertTrue(created.created)

                login = service.create_or_login(username='alice', password='pass123')
                self.assertTrue(login.ok)
                self.assertFalse(login.created)

                mismatch = service.create_or_login(username='alice', password='wrong')
                self.assertFalse(mismatch.ok)
                self.assertIn('Password mismatch', mismatch.error_message)

            with sqlite3.connect(database_path) as connection:
                row = connection.execute(
                    'SELECT password FROM users WHERE username = ?',
                    ('alice',),
                ).fetchone()
            self.assertIsNotNone(row)
            self.assertTrue(str(row[0]).startswith('v1$'))
            self.assertNotEqual(str(row[0]), 'pass123')


    def test_kage_login_key_is_sealed_and_bound_to_the_server_secret(self) -> None:
        with tempfile.TemporaryDirectory() as temp_directory:
            database_path = f'{temp_directory}/kage.sqlite'
            with patch.dict(os.environ, {'OPENSNAP_SQLITE_PATH': database_path}, clear=True):
                storage = create_storage(default_app_config())
                service = SignupService(storage.accounts)
                self.assertTrue(service.create_or_login(username='alice', password='pass123', store_kage_login_key=True).ok)
                self.assertEqual(storage.accounts.get_by_name('alice').kage_login_key, b'pass123')
                storage.close()

            with sqlite3.connect(database_path) as connection:
                stored = connection.execute('SELECT kage_login_key FROM users').fetchone()[0]
            self.assertNotIn('pass123', stored)

            # Another server secret cannot read it: the account behaves as if none was stored.
            with patch.dict(
                os.environ,
                {'OPENSNAP_SQLITE_PATH': database_path, 'OPENSNAP_SERVER_SECRET': 'another secret'},
                clear=True,
            ):
                storage = create_storage(default_app_config())
                self.addCleanup(storage.close)
                self.assertEqual(storage.accounts.get_by_name('alice').kage_login_key, b'')

    def test_existing_store_gains_the_kage_login_key_column(self) -> None:
        with tempfile.TemporaryDirectory() as temp_directory:
            database_path = f'{temp_directory}/old.sqlite'
            with sqlite3.connect(database_path) as connection:
                connection.execute(
                    'CREATE TABLE users (user_id INTEGER PRIMARY KEY, username TEXT NOT NULL UNIQUE, '
                    "password TEXT NOT NULL, seed TEXT NOT NULL, team TEXT NOT NULL DEFAULT '')"
                )
                connection.execute("INSERT INTO users (username, password, seed) VALUES ('old', 'pw12', 'seed')")
            with patch.dict(os.environ, {'OPENSNAP_SQLITE_PATH': database_path}, clear=True):
                storage = create_storage(default_app_config())
                self.addCleanup(storage.close)
                account = storage.accounts.get_by_name('old')
                self.assertIsNotNone(account)
                self.assertEqual(account.kage_login_key, b'')

if __name__ == '__main__':
    unittest.main()
