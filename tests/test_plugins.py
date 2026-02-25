"""Plugin resolution and configuration tests."""

import os
import unittest
from unittest.mock import patch

from opensnap.config import default_app_config
from opensnap.plugins import create_game_plugin, list_game_plugins
from opensnap.plugins.automodellista import AutoModellistaPlugin


class PluginSelectionTests(unittest.TestCase):
    """Verify game plugin selection behavior."""

    def test_default_plugin_config_is_automodellista(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop('OPENSNAP_GAME_PLUGIN', None)
            config = default_app_config()

        self.assertEqual(config.server.game_plugin, 'automodellista')

    def test_plugin_config_reads_environment_override(self) -> None:
        with patch.dict(os.environ, {'OPENSNAP_GAME_PLUGIN': 'AUTOMODELLISTA'}, clear=False):
            config = default_app_config()

        self.assertEqual(config.server.game_plugin, 'automodellista')

    def test_create_game_plugin_returns_registered_plugin(self) -> None:
        plugin = create_game_plugin('automodellista')
        self.assertIsInstance(plugin, AutoModellistaPlugin)
        self.assertIn('automodellista', list_game_plugins())

    def test_create_game_plugin_rejects_unknown_name(self) -> None:
        with self.assertRaises(ValueError):
            create_game_plugin('unknown-game')


if __name__ == '__main__':
    unittest.main()
