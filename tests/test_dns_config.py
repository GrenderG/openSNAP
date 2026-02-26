"""DNS configuration tests."""

import os
import unittest
from unittest.mock import patch

from opensnap_dns.config import DEFAULT_DNS_ENTRIES, default_dns_server_config


class DnsConfigTests(unittest.TestCase):
    """Verify env-driven DNS config behavior."""

    def test_default_dns_entries_are_present(self) -> None:
        with patch.dict(
            os.environ,
            {
                'OPENSNAP_DNS_ENTRIES': '{}',
                'OPENSNAP_DNS_DEFAULT_IP': '192.168.1.151',
            },
            clear=True,
        ):
            config = default_dns_server_config()

        for domain in DEFAULT_DNS_ENTRIES:
            self.assertIn(domain, config.entries)
            self.assertEqual(config.entries[domain], '192.168.1.151')

    def test_dns_entries_json_override_defaults_and_add_new_names(self) -> None:
        with patch.dict(
            os.environ,
            {
                'OPENSNAP_DNS_DEFAULT_IP': '192.168.1.151',
                'OPENSNAP_DNS_ENTRIES': (
                    '{"bootstrap.capcom-am.games.sega.net":"10.0.0.2",'
                    '"custom.example.net":"10.0.0.9"}'
                ),
            },
            clear=True,
        ):
            config = default_dns_server_config()

        self.assertEqual(config.entries['bootstrap.capcom-am.games.sega.net'], '10.0.0.2')
        self.assertEqual(config.entries['custom.example.net'], '10.0.0.9')
        self.assertEqual(config.entries['gameweb.capcom-am.games.sega.net'], '192.168.1.151')

    def test_dns_entries_accept_python_dict_literal(self) -> None:
        with patch.dict(
            os.environ,
            {
                'OPENSNAP_DNS_DEFAULT_IP': '192.168.1.151',
                'OPENSNAP_DNS_ENTRIES': (
                    "{'regweb.capcom-am.games.sega.net': '10.0.0.3', 'broken': 'not-an-ip'}"
                ),
            },
            clear=True,
        ):
            config = default_dns_server_config()

        self.assertEqual(config.entries['regweb.capcom-am.games.sega.net'], '10.0.0.3')
        self.assertNotIn('broken', config.entries)

    def test_dns_entries_support_default_alias(self) -> None:
        with patch.dict(
            os.environ,
            {
                'OPENSNAP_DNS_DEFAULT_IP': '10.0.0.8',
                'OPENSNAP_DNS_ENTRIES': '{"custom.example.net":"@default"}',
            },
            clear=True,
        ):
            config = default_dns_server_config()

        self.assertEqual(config.entries['custom.example.net'], '10.0.0.8')

    def test_monster_hunter_default_dns_entries_are_present(self) -> None:
        with patch.dict(
            os.environ,
            {
                'OPENSNAP_DNS_ENTRIES': '{}',
                'OPENSNAP_DNS_DEFAULT_IP': '192.168.1.151',
            },
            clear=True,
        ):
            config = default_dns_server_config()

        self.assertEqual(config.entries['regweb.mh.capcom.sf.yav4.com'], '192.168.1.151')
        self.assertEqual(config.entries['bootstrap01.sf.yav4.com'], '192.168.1.151')
        self.assertEqual(config.entries['bootstrap01.mheu-beta.capcom.sf.yav.com'], '192.168.1.151')
        self.assertEqual(config.entries['regweb.reo.capcom.sf.yav4.com'], '192.168.1.151')
        self.assertEqual(config.entries['snap01.reo.capcom.sf.yav4.com'], '192.168.1.151')


if __name__ == '__main__':
    unittest.main()
