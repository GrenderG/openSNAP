"""Core configuration tests."""

import os
import unittest
from unittest.mock import patch

from opensnap.config import default_app_config


class AppConfigTests(unittest.TestCase):
    """Verify env-driven app config behavior."""

    def test_default_users_include_test_user(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            config = default_app_config()

        usernames = {user.username for user in config.users}
        self.assertIn('test', usernames)

    def test_default_users_can_be_overridden_from_env(self) -> None:
        with patch.dict(
            os.environ,
            {'OPENSNAP_DEFAULT_USERS': 'alice:pw,bob:pw2:seed-b:team-b'},
            clear=True,
        ):
            config = default_app_config()

        self.assertEqual(len(config.users), 2)
        self.assertEqual(config.users[0].user_id, 1)
        self.assertEqual(config.users[0].username, 'alice')
        self.assertEqual(config.users[0].password, 'pw')
        self.assertEqual(config.users[0].seed, '')
        self.assertEqual(config.users[0].team, '')
        self.assertEqual(config.users[1].user_id, 2)
        self.assertEqual(config.users[1].username, 'bob')
        self.assertEqual(config.users[1].seed, 'seed-b')
        self.assertEqual(config.users[1].team, 'team-b')

    def test_invalid_default_user_env_falls_back_to_defaults(self) -> None:
        with patch.dict(os.environ, {'OPENSNAP_DEFAULT_USERS': 'invalid-entry'}, clear=True):
            config = default_app_config()

        usernames = {user.username for user in config.users}
        self.assertIn('test', usernames)
        self.assertEqual(usernames, {'test'})

    def test_default_storage_backend_is_sqlite(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            config = default_app_config()

        self.assertEqual(config.storage.backend, 'sqlite')


if __name__ == '__main__':
    unittest.main()
