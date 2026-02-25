"""Signup service tests."""

import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from opensnap_web.signup import SqliteSignupService


class SqliteSignupServiceTests(unittest.TestCase):
    """Verify create/login behavior backed by SQLite."""

    def test_create_then_login_then_wrong_password(self) -> None:
        with tempfile.TemporaryDirectory() as temp_directory:
            database_path = f'{temp_directory}/web-signup.sqlite'
            with patch.dict(
                os.environ,
                {
                    'OPENSNAP_SQLITE_PATH': database_path,
                    'OPENSNAP_DEFAULT_USERS': 'test:1111',
                },
                clear=True,
            ):
                service = SqliteSignupService()

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

    def test_default_user_from_env_exists(self) -> None:
        with tempfile.TemporaryDirectory() as temp_directory:
            database_path = f'{temp_directory}/web-default-users.sqlite'
            with patch.dict(
                os.environ,
                {
                    'OPENSNAP_SQLITE_PATH': database_path,
                    'OPENSNAP_DEFAULT_USERS': 'test:1111',
                },
                clear=True,
            ):
                service = SqliteSignupService()
                result = service.create_or_login(username='test', password='1111')
                self.assertTrue(result.ok)
                self.assertFalse(result.created)


if __name__ == '__main__':
    unittest.main()
