"""Socket address-reuse configuration tests."""

import logging
import socket
import unittest
from unittest.mock import Mock

from opensnap.config import ServerConfig
from opensnap.udp_server import SnapUdpServer

try:
    from opensnap_web.server import _enable_web_reuse_address
    from werkzeug.serving import BaseWSGIServer, ThreadedWSGIServer
except ModuleNotFoundError:  # pragma: no cover
    _enable_web_reuse_address = None
    BaseWSGIServer = None
    ThreadedWSGIServer = None


class _FakeSocket:
    """Simple socket double for setsockopt calls."""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[tuple[int, int, int]] = []

    def setsockopt(self, level: int, optname: int, value: int) -> None:
        if self.fail:
            raise OSError('boom')
        self.calls.append((level, optname, value))


class SocketReuseTests(unittest.TestCase):
    """Verify socket reuse is explicitly enabled for services."""

    def test_udp_reuse_address_is_enabled(self) -> None:
        server = SnapUdpServer(config=ServerConfig(), engine=Mock())
        fake_socket = _FakeSocket()

        server._enable_reuse_address(fake_socket)  # type: ignore[arg-type]

        self.assertEqual(len(fake_socket.calls), 1)
        level, optname, value = fake_socket.calls[0]
        self.assertEqual(level, socket.SOL_SOCKET)
        self.assertEqual(optname, socket.SO_REUSEADDR)
        self.assertEqual(value, 1)

    def test_udp_reuse_address_failure_logs_warning(self) -> None:
        server = SnapUdpServer(config=ServerConfig(), engine=Mock())
        fake_socket = _FakeSocket(fail=True)

        with self.assertLogs('opensnap.game', level='WARNING') as captured:
            server._enable_reuse_address(fake_socket)  # type: ignore[arg-type]

        self.assertIn('Failed to enable SO_REUSEADDR', '\n'.join(captured.output))


@unittest.skipIf(_enable_web_reuse_address is None, 'Flask/Werkzeug is not installed.')
class WebReuseTests(unittest.TestCase):
    """Verify Werkzeug reuse behavior is set by startup code."""

    def test_web_reuse_address_is_enabled(self) -> None:
        assert BaseWSGIServer is not None
        assert ThreadedWSGIServer is not None

        old_base = BaseWSGIServer.allow_reuse_address
        old_threaded = ThreadedWSGIServer.allow_reuse_address
        logger = logging.getLogger('opensnap.web')
        try:
            BaseWSGIServer.allow_reuse_address = False
            ThreadedWSGIServer.allow_reuse_address = False
            _enable_web_reuse_address(logger)
            self.assertTrue(BaseWSGIServer.allow_reuse_address)
            self.assertTrue(ThreadedWSGIServer.allow_reuse_address)
        finally:
            BaseWSGIServer.allow_reuse_address = old_base
            ThreadedWSGIServer.allow_reuse_address = old_threaded


if __name__ == '__main__':
    unittest.main()
