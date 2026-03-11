"""Runtime-loop failure tests for the UDP server."""

import unittest
from unittest.mock import Mock, patch

from opensnap.config import ServerConfig
from opensnap.udp_server import SnapUdpServer


class _FailingRecvSocket:
    """Socket double that fails on the first `recvfrom`."""

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


class UdpServerRuntimeTests(unittest.TestCase):
    """Verify fatal socket-loop failures do not exit silently."""

    def test_run_logs_and_reraises_recvfrom_os_error(self) -> None:
        engine = Mock()
        server = SnapUdpServer(config=ServerConfig(), engine=engine)
        fake_socket = _FailingRecvSocket()

        with self.assertLogs('opensnap.game', level='ERROR') as captured:
            with self.assertRaises(OSError) as raised:
                with patch('opensnap.udp_server.socket.socket', return_value=fake_socket):
                    server.run()

        self.assertEqual(str(raised.exception), 'network down')
        engine.close.assert_called_once_with()
        joined = '\n'.join(captured.output)
        self.assertIn(
            f'UDP recvfrom failed on {server._config.host}:{server._config.port}: network down',
            joined,
        )


if __name__ == '__main__':
    unittest.main()
