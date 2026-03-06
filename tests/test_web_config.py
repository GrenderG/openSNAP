"""Web configuration tests."""

import os
import unittest
from unittest.mock import patch

from opensnap_web.config import default_web_server_config


class WebConfigTests(unittest.TestCase):
    """Verify default and overridden web config behavior."""

    def test_default_web_port_is_80(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop('OPENSNAP_WEB_PORT', None)
            config = default_web_server_config()

        self.assertEqual(config.port, 80)

    def test_invalid_web_port_falls_back_to_80(self) -> None:
        with patch.dict(os.environ, {'OPENSNAP_WEB_PORT': 'invalid'}, clear=False):
            config = default_web_server_config()

        self.assertEqual(config.port, 80)

    def test_valid_web_port_override(self) -> None:
        with patch.dict(os.environ, {'OPENSNAP_WEB_PORT': '8081'}, clear=False):
            config = default_web_server_config()

        self.assertEqual(config.port, 8081)

    def test_default_web_profile_is_generic(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop('OPENSNAP_WEB_GAME_PLUGIN', None)
            config = default_web_server_config()

        self.assertEqual(config.game_plugin, 'generic')

    def test_default_https_settings_follow_web_defaults(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop('OPENSNAP_WEB_HTTPS_HOST', None)
            os.environ.pop('OPENSNAP_WEB_HTTPS_PORT', None)
            os.environ.pop('OPENSNAP_WEB_HTTPS_CERTFILE', None)
            os.environ.pop('OPENSNAP_WEB_HTTPS_KEYFILE', None)
            config = default_web_server_config()

        self.assertEqual(config.https_host, config.host)
        self.assertEqual(config.https_port, 443)
        self.assertFalse(config.https_enabled)

    def test_invalid_https_port_falls_back_to_443(self) -> None:
        with patch.dict(os.environ, {'OPENSNAP_WEB_HTTPS_PORT': 'invalid'}, clear=False):
            config = default_web_server_config()

        self.assertEqual(config.https_port, 443)

    def test_https_listener_is_enabled_only_with_cert_and_key(self) -> None:
        with patch.dict(
            os.environ,
            {
                'OPENSNAP_WEB_HTTPS_CERTFILE': '/tmp/test-cert.pem',
                'OPENSNAP_WEB_HTTPS_KEYFILE': '/tmp/test-key.pem',
            },
            clear=False,
        ):
            config = default_web_server_config()

        self.assertTrue(config.https_enabled)
        self.assertEqual(config.https_certfile, '/tmp/test-cert.pem')
        self.assertEqual(config.https_keyfile, '/tmp/test-key.pem')


if __name__ == '__main__':
    unittest.main()
