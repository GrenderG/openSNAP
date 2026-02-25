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


if __name__ == '__main__':
    unittest.main()
