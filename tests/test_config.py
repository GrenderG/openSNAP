"""Core configuration tests."""

import os
import unittest
from unittest.mock import patch

from opensnap.config import (
    DEFAULT_MAX_LOBBIES,
    DEFAULT_MAX_PLAYERS_PER_ROOM,
    DEFAULT_MAX_ROOMS_PER_LOBBY,
    default_app_config,
)


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

    def test_runtime_reset_flag_can_be_overridden(self) -> None:
        with patch.dict(
            os.environ,
            {'OPENSNAP_RESET_RUNTIME_ON_STARTUP': 'false'},
            clear=True,
        ):
            config = default_app_config()

        self.assertFalse(config.storage.reset_runtime_on_startup)

    def test_game_advertise_host_can_use_compatibility_env_names(self) -> None:
        with patch.dict(
            os.environ,
            {
                'OPENSNAP_HOST': '0.0.0.0',
                'OPENSNAP_ADVERTISE_HOST': '192.168.1.151',
                'OPENSNAP_PORT': '10070',
            },
            clear=True,
        ):
            config = default_app_config()

        self.assertEqual(config.server.game.host, '0.0.0.0')
        self.assertEqual(config.server.game.advertise_host, '192.168.1.151')
        self.assertEqual(config.server.game.port, 10070)
        self.assertEqual(config.server.host, '0.0.0.0')
        self.assertEqual(config.server.advertise_host, '192.168.1.151')
        self.assertEqual(config.server.port, 10070)

    def test_bootstrap_and_game_endpoints_can_be_configured_independently(self) -> None:
        with patch.dict(
            os.environ,
            {
                'OPENSNAP_BOOTSTRAP_HOST': '10.0.0.1',
                'OPENSNAP_BOOTSTRAP_ADVERTISE_HOST': '203.0.113.10',
                'OPENSNAP_BOOTSTRAP_PORT': '9090',
                'OPENSNAP_GAME_HOST': '10.0.0.2',
                'OPENSNAP_GAME_ADVERTISE_HOST': '203.0.113.20',
                'OPENSNAP_GAME_PORT': '10070',
            },
            clear=True,
        ):
            config = default_app_config()

        self.assertEqual(config.server.bootstrap.host, '10.0.0.1')
        self.assertEqual(config.server.bootstrap.advertise_host, '203.0.113.10')
        self.assertEqual(config.server.bootstrap.port, 9090)
        self.assertEqual(config.server.game.host, '10.0.0.2')
        self.assertEqual(config.server.game.advertise_host, '203.0.113.20')
        self.assertEqual(config.server.game.port, 10070)

    def test_game_server_map_can_override_bootstrap_targets(self) -> None:
        with patch.dict(
            os.environ,
            {
                'OPENSNAP_GAME_IDENTIFIER': 'automodellista-release',
                'OPENSNAP_BOOTSTRAP_DEFAULT_GAME_IDENTIFIER': 'monsterhunter',
                'OPENSNAP_GAME_SERVER_MAP': (
                    '{"monsterhunter":{"host":"203.0.113.90","port":10090},'
                    '"automodellista-release":"203.0.113.20:10070"}'
                ),
            },
            clear=True,
        ):
            config = default_app_config()

        self.assertEqual(config.server.game_identifier, 'automodellista-release')
        self.assertEqual(config.server.default_bootstrap_game_identifier, 'monsterhunter')
        monsterhunter_target = config.server.resolve_game_target('monsterhunter')
        automodellista_target = config.server.resolve_game_target('automodellista-release')
        assert monsterhunter_target is not None
        assert automodellista_target is not None
        self.assertEqual(monsterhunter_target.host, '203.0.113.90')
        self.assertEqual(monsterhunter_target.port, 10090)
        self.assertEqual(automodellista_target.host, '203.0.113.20')
        self.assertEqual(automodellista_target.port, 10070)

    def test_game_server_map_accepts_game_to_host_port_strings(self) -> None:
        with patch.dict(
            os.environ,
            {
                'OPENSNAP_GAME_IDENTIFIER': 'automodellista',
                'OPENSNAP_GAME_SERVER_MAP': (
                    '{"automodellista":"192.168.1.151:9091",'
                    '"monsterhunter":"192.168.1.152:10070"}'
                ),
            },
            clear=True,
        ):
            config = default_app_config()

        automodellista_target = config.server.resolve_game_target('automodellista')
        monsterhunter_target = config.server.resolve_game_target('monsterhunter')
        assert automodellista_target is not None
        assert monsterhunter_target is not None
        self.assertEqual(automodellista_target.host, '192.168.1.151')
        self.assertEqual(automodellista_target.port, 9091)
        self.assertEqual(monsterhunter_target.host, '192.168.1.152')
        self.assertEqual(monsterhunter_target.port, 10070)

    def test_server_limit_defaults_are_used_when_env_is_unset(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            config = default_app_config()

        self.assertEqual(config.server.max_lobbies, DEFAULT_MAX_LOBBIES)
        self.assertEqual(config.server.max_rooms_per_lobby, DEFAULT_MAX_ROOMS_PER_LOBBY)
        self.assertEqual(config.server.max_players_per_room, DEFAULT_MAX_PLAYERS_PER_ROOM)
        self.assertEqual(len(config.lobbies), DEFAULT_MAX_LOBBIES)

    def test_server_limits_can_be_overridden_from_env(self) -> None:
        with patch.dict(
            os.environ,
            {
                'OPENSNAP_MAX_LOBBIES': '6',
                'OPENSNAP_MAX_ROOMS_PER_LOBBY': '12',
                'OPENSNAP_MAX_PLAYERS_PER_ROOM': '7',
            },
            clear=True,
        ):
            config = default_app_config()

        self.assertEqual(config.server.max_lobbies, 6)
        self.assertEqual(config.server.max_rooms_per_lobby, 12)
        self.assertEqual(config.server.max_players_per_room, 7)
        self.assertEqual(len(config.lobbies), 6)

    def test_non_positive_server_limits_fall_back_to_defaults(self) -> None:
        with patch.dict(
            os.environ,
            {
                'OPENSNAP_MAX_LOBBIES': '0',
                'OPENSNAP_MAX_ROOMS_PER_LOBBY': '-5',
                'OPENSNAP_MAX_PLAYERS_PER_ROOM': '0',
            },
            clear=True,
        ):
            config = default_app_config()

        self.assertEqual(config.server.max_lobbies, DEFAULT_MAX_LOBBIES)
        self.assertEqual(config.server.max_rooms_per_lobby, DEFAULT_MAX_ROOMS_PER_LOBBY)
        self.assertEqual(config.server.max_players_per_room, DEFAULT_MAX_PLAYERS_PER_ROOM)
        self.assertEqual(len(config.lobbies), DEFAULT_MAX_LOBBIES)


if __name__ == '__main__':
    unittest.main()
