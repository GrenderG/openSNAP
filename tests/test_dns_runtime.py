"""Runtime-loop failure tests for the DNS server."""

import unittest
from unittest.mock import Mock, patch

try:
    from opensnap_dns.config import DnsServerConfig
    from opensnap_dns.server import SnapDnsServer
except ModuleNotFoundError:  # pragma: no cover
    DnsServerConfig = None
    SnapDnsServer = None


class _FailingRecvSocket:
    """Socket double that fails on the first DNS `recvfrom`."""

    def __init__(self) -> None:
        self.bound: tuple[str, int] | None = None
        self.timeout: float | None = None

    def __enter__(self) -> "_FailingRecvSocket":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False

    def setsockopt(self, level: int, optname: int, value: int) -> None:
        return None

    def bind(self, target: tuple[str, int]) -> None:
        self.bound = target

    def settimeout(self, timeout: float) -> None:
        self.timeout = timeout

    def recvfrom(self, size: int) -> tuple[bytes, tuple[str, int]]:
        raise OSError('network down')


@unittest.skipIf(SnapDnsServer is None, 'dnslib is not installed.')
class DnsRuntimeTests(unittest.TestCase):
    """Verify DNS socket-loop failures do not exit silently."""

    def test_run_logs_and_reraises_recvfrom_os_error(self) -> None:
        assert DnsServerConfig is not None
        server = SnapDnsServer(config=DnsServerConfig(entries={}, ttl=60))
        fake_socket = _FailingRecvSocket()

        with self.assertLogs('opensnap.dns', level='ERROR') as captured:
            with self.assertRaises(OSError) as raised:
                with patch('opensnap_dns.server.socket.socket', return_value=fake_socket):
                    server.run()

        self.assertEqual(str(raised.exception), 'network down')
        joined = '\n'.join(captured.output)
        self.assertIn(
            f'DNS recvfrom failed on {server._config.host}:{server._config.port}: network down',
            joined,
        )


if __name__ == '__main__':
    unittest.main()
