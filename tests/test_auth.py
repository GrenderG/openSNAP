"""Bootstrap-auth helper tests."""

import unittest
from unittest.mock import patch

from opensnap.core.auth import _resolve_advertise_host


class BootstrapAuthHelpersTests(unittest.TestCase):
    """Verify advertised-host resolution for login-success payloads."""

    def test_resolve_advertise_host_prefers_explicit_config(self) -> None:
        value = _resolve_advertise_host(
            configured_host='192.168.1.200',
            bind_host='0.0.0.0',
            client_host='192.168.1.151',
        )
        self.assertEqual(value, '192.168.1.200')

    def test_resolve_advertise_host_uses_specific_bind_host(self) -> None:
        value = _resolve_advertise_host(
            configured_host='',
            bind_host='192.168.1.151',
            client_host='192.168.1.199',
        )
        self.assertEqual(value, '192.168.1.151')

    def test_resolve_advertise_host_uses_routed_local_host_for_wildcard_bind(self) -> None:
        with patch('opensnap.core.auth._resolve_local_host_for_client', return_value='192.168.1.151'):
            value = _resolve_advertise_host(
                configured_host='',
                bind_host='0.0.0.0',
                client_host='192.168.1.199',
            )
        self.assertEqual(value, '192.168.1.151')

    def test_resolve_advertise_host_falls_back_to_loopback_when_unresolved(self) -> None:
        with patch('opensnap.core.auth._resolve_local_host_for_client', return_value=''):
            value = _resolve_advertise_host(
                configured_host='',
                bind_host='0.0.0.0',
                client_host='192.168.1.199',
            )
        self.assertEqual(value, '127.0.0.1')


if __name__ == '__main__':
    unittest.main()
