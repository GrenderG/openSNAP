"""Runtime-loop failure tests for the DNS server."""

import errno
import socket
import unittest
from unittest.mock import patch

try:
    from opensnap_dns.config import DnsServerConfig
    from opensnap_dns.server import SnapDnsServer
except ModuleNotFoundError:  # pragma: no cover
    DnsServerConfig = None
    SnapDnsServer = None


class _TransientRecvSocket:
    """Socket double that emits one recoverable DNS recv error, then stops."""

    def __init__(self, error: OSError, stop_callback) -> None:
        self.bound: tuple[str, int] | None = None
        self.timeout: float | None = None
        self.error = error
        self.stop_callback = stop_callback
        self.recv_calls = 0

    def __enter__(self) -> "_TransientRecvSocket":
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
        self.recv_calls += 1
        if self.recv_calls == 1:
            raise self.error
        self.stop_callback()
        raise socket.timeout()


@unittest.skipIf(SnapDnsServer is None, 'dnslib is not installed.')
class DnsRuntimeTests(unittest.TestCase):
    """Verify DNS socket-loop errors stay visible without killing the loop."""

    def test_run_logs_and_continues_after_recvfrom_os_error(self) -> None:
        assert DnsServerConfig is not None

        runtime_errors = (
            BlockingIOError(errno.EAGAIN, 'Resource temporarily unavailable'),
            OSError('network down'),
            OSError(errno.EBADF, 'Bad file descriptor'),
        )

        for error in runtime_errors:
            with self.subTest(error=str(error)):
                server = SnapDnsServer(config=DnsServerConfig(entries={}, ttl=60))
                fake_socket = _TransientRecvSocket(error, server.stop)

                with self.assertLogs('opensnap.dns', level='ERROR') as captured:
                    with patch('opensnap_dns.server.socket.socket', return_value=fake_socket):
                        server.run()

                self.assertEqual(fake_socket.recv_calls, 2)
                self.assertIn('DNS recvfrom failed on', '\n'.join(captured.output))


if __name__ == '__main__':
    unittest.main()
