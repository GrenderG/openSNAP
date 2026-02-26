"""Standalone DNS server behavior tests."""

import socket
import unittest
from unittest.mock import patch

try:
    from dnslib import DNSRecord, QTYPE, RCODE
    from opensnap_dns.config import DnsServerConfig
    from opensnap_dns.server import SnapDnsServer
except ModuleNotFoundError:  # pragma: no cover
    DNSRecord = None
    QTYPE = None
    RCODE = None
    DnsServerConfig = None
    SnapDnsServer = None


class _FakeSocket:
    """Simple socket double for setsockopt calls."""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[tuple[int, int, int]] = []

    def setsockopt(self, level: int, optname: int, value: int) -> None:
        if self.fail:
            raise OSError('boom')
        self.calls.append((level, optname, value))


@unittest.skipIf(SnapDnsServer is None, 'dnslib is not installed.')
class DnsServerTests(unittest.TestCase):
    """Verify DNS request handling and socket setup."""

    def test_dns_a_query_returns_configured_record(self) -> None:
        assert DnsServerConfig is not None
        assert DNSRecord is not None
        assert RCODE is not None

        server = SnapDnsServer(
            config=DnsServerConfig(
                entries={'bootstrap.capcom-am.games.sega.net': '192.168.1.151'},
                ttl=123,
            )
        )
        request = DNSRecord.question('bootstrap.capcom-am.games.sega.net', qtype='A').pack()

        with self.assertLogs('opensnap.dns', level='INFO') as captured:
            response_raw = server._build_response(request)

        self.assertIsNotNone(response_raw)
        response = DNSRecord.parse(response_raw)
        self.assertEqual(response.header.rcode, RCODE.NOERROR)
        self.assertEqual(len(response.rr), 1)
        self.assertEqual(str(response.rr[0].rdata), '192.168.1.151')
        self.assertEqual(response.rr[0].ttl, 123)
        self.assertIn(
            'Received DNS request for: bootstrap.capcom-am.games.sega.net',
            '\n'.join(captured.output),
        )

    def test_dns_unknown_name_returns_nxdomain(self) -> None:
        assert DnsServerConfig is not None
        assert DNSRecord is not None
        assert RCODE is not None

        server = SnapDnsServer(config=DnsServerConfig(entries={}, ttl=60))
        request = DNSRecord.question('unknown.example.net', qtype='A').pack()
        response_raw = server._build_response(request)

        self.assertIsNotNone(response_raw)
        response = DNSRecord.parse(response_raw)
        self.assertEqual(response.header.rcode, RCODE.NXDOMAIN)
        self.assertEqual(len(response.rr), 0)

    def test_dns_wildcard_entry_matches_subdomain(self) -> None:
        assert DnsServerConfig is not None
        assert DNSRecord is not None
        assert RCODE is not None

        server = SnapDnsServer(config=DnsServerConfig(entries={'*.games.sega.net': '10.20.30.40'}, ttl=60))
        request = DNSRecord.question('foo.games.sega.net', qtype='A').pack()
        response_raw = server._build_response(request)

        self.assertIsNotNone(response_raw)
        response = DNSRecord.parse(response_raw)
        self.assertEqual(response.header.rcode, RCODE.NOERROR)
        self.assertEqual(len(response.rr), 1)
        self.assertEqual(str(response.rr[0].rdata), '10.20.30.40')

    def test_dns_unknown_name_uses_system_resolver(self) -> None:
        assert DnsServerConfig is not None
        assert DNSRecord is not None
        assert RCODE is not None

        server = SnapDnsServer(config=DnsServerConfig(entries={}, ttl=90))
        request = DNSRecord.question('fallback.example.net', qtype='A').pack()
        with patch(
            'socket.getaddrinfo',
            return_value=[
                (socket.AF_INET, socket.SOCK_DGRAM, 17, '', ('8.8.8.8', 0)),
                (socket.AF_INET, socket.SOCK_DGRAM, 17, '', ('1.1.1.1', 0)),
            ],
        ):
            response_raw = server._build_response(request)

        self.assertIsNotNone(response_raw)
        response = DNSRecord.parse(response_raw)
        self.assertEqual(response.header.rcode, RCODE.NOERROR)
        self.assertEqual(len(response.rr), 2)
        self.assertEqual({str(answer.rdata) for answer in response.rr}, {'8.8.8.8', '1.1.1.1'})
        self.assertEqual({answer.ttl for answer in response.rr}, {90})

    def test_dns_reuse_address_is_enabled(self) -> None:
        assert DnsServerConfig is not None

        server = SnapDnsServer(config=DnsServerConfig(entries={}, ttl=60))
        fake_socket = _FakeSocket()

        server._enable_reuse_address(fake_socket)  # type: ignore[arg-type]

        self.assertEqual(len(fake_socket.calls), 1)
        level, optname, value = fake_socket.calls[0]
        self.assertEqual(level, socket.SOL_SOCKET)
        self.assertEqual(optname, socket.SO_REUSEADDR)
        self.assertEqual(value, 1)


if __name__ == '__main__':
    unittest.main()
