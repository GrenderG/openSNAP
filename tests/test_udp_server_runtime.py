"""Runtime-loop failure tests for the UDP server."""

import errno
import unittest
from unittest.mock import Mock, patch

from opensnap.config import ServerConfig
from opensnap.protocol import commands
from opensnap.protocol.constants import FLAG_RELIABLE, FLAG_ROOM
from opensnap.protocol.models import Endpoint
from opensnap.protocol.models import SnapMessage
from opensnap.udp_server import SnapUdpServer


class _TransientRecvSocket:
    """Socket double that transiently reports one recoverable recv error."""

    def __init__(self, error: OSError) -> None:
        self.bound: tuple[str, int] | None = None
        self.timeout: float | None = None
        self.recv_calls = 0
        self.error = error

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
        raise self.error


class _FailingSendSocket:
    """Socket double that fails every `sendto` with one configured error."""

    def __init__(self, error: OSError) -> None:
        self.error = error

    def sendto(self, payload: bytes, target: tuple[str, int]) -> None:
        raise self.error


class UdpServerRuntimeTests(unittest.TestCase):
    """Verify runtime socket-loop errors stay visible without killing the loop."""

    def test_poll_timeout_uses_idle_fallback_without_pending_reliable(self) -> None:
        server = SnapUdpServer(config=ServerConfig(), engine=Mock())

        timeout = server._poll_timeout_seconds(10.0, 20.0)

        self.assertEqual(timeout, server._IDLE_SOCKET_POLL_SECONDS)

    def test_poll_timeout_tracks_next_due_retransmit_when_pending_exists(self) -> None:
        server = SnapUdpServer(config=ServerConfig(), engine=Mock())
        endpoint = Endpoint(host='127.0.0.1', port=50000)
        outgoing = SnapMessage(
            endpoint=endpoint,
            type_flags=FLAG_ROOM | FLAG_RELIABLE,
            packet_number=0,
            command=commands.CMD_SEND,
            session_id=0x12345678,
            sequence_number=9,
            acknowledge_number=0,
            payload=b'\x80\x07' + b'\x00' * 62,
        )
        server._track_reliable(outgoing, b'packet', 9.95)

        timeout = server._poll_timeout_seconds(10.0, 20.0)

        self.assertAlmostEqual(timeout, 0.15, places=3)

    def test_run_logs_and_continues_after_recvfrom_os_error(self) -> None:
        runtime_errors = (
            BlockingIOError(errno.EAGAIN, 'Resource temporarily unavailable'),
            OSError('network down'),
            OSError(errno.EBADF, 'Bad file descriptor'),
        )

        for error in runtime_errors:
            with self.subTest(error=str(error)):
                engine = Mock()
                server = SnapUdpServer(config=ServerConfig(), engine=engine, tick_interval_seconds=0.0)
                fake_socket = _TransientRecvSocket(error)

                def _stop_on_tick() -> list[object]:
                    server.stop()
                    return []

                engine.tick.side_effect = _stop_on_tick

                with self.assertLogs('opensnap.game', level='ERROR') as captured:
                    with patch('opensnap.udp_server.socket.socket', return_value=fake_socket):
                        server.run()

                self.assertGreaterEqual(fake_socket.recv_calls, 1)
                engine.tick.assert_called()
                engine.close.assert_called_once_with()
                self.assertIn('UDP recvfrom failed on', '\n'.join(captured.output))

    def test_send_socket_datagram_logs_and_continues_on_os_error(self) -> None:
        send_errors = (
            OSError(errno.ENOBUFS, 'No buffer space available'),
            OSError(errno.EBADF, 'Bad file descriptor'),
        )

        for error in send_errors:
            with self.subTest(error=str(error)):
                engine = Mock()
                server = SnapUdpServer(config=ServerConfig(), engine=engine)
                endpoint = Endpoint(host='127.0.0.1', port=50000)
                fake_socket = _FailingSendSocket(error)

                with self.assertLogs('opensnap.game', level='ERROR') as captured:
                    sent = server._send_socket_datagram(
                        fake_socket,  # type: ignore[arg-type]
                        datagram=b'test',
                        endpoint=endpoint,
                        action='unit-test send',
                    )

                self.assertFalse(sent)
                self.assertIn('UDP sendto failed during unit-test send', '\n'.join(captured.output))

if __name__ == '__main__':
    unittest.main()
