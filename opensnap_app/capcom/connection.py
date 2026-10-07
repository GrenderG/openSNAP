"""Framed request/reply helper around one Capcom APP client socket."""

import logging
import socket
import time

from opensnap_app.capcom.protocol import (
    APP_DIRECTION_REPLY,
    APP_FRAME_HEADER_SIZE,
    app_frame,
    frame_command,
    frame_sequence,
    pop_app_frame,
)


class AppConnection:
    """One client connection: buffered frame reads, frame writes and a closing linger."""

    def __init__(self, client: socket.socket, address: tuple[str, int], logger: logging.Logger) -> None:
        self._client = client
        self._buffer = bytearray()
        self._logger = logger
        self.host = address[0]
        self.port = address[1]

    def set_timeout(self, seconds: float) -> None:
        """Fail any later read or write that waits longer than `seconds`."""

        self._client.settimeout(seconds)

    def poll(self, seconds: float) -> bool:
        """Return whether a complete frame arrives within `seconds` (it stays buffered)."""

        deadline = time.monotonic() + seconds
        previous = self._client.gettimeout()
        try:
            while not self._has_frame():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._client.settimeout(remaining)
                try:
                    data = self._client.recv(65535)
                except socket.timeout:
                    return False
                if not data:
                    raise ConnectionError('client closed the connection')
                self._buffer.extend(data)
            return True
        finally:
            self._client.settimeout(previous)

    def peek_command(self) -> int:
        """Command of the buffered frame `poll` reported."""

        return int.from_bytes(self._buffer[3:5], 'big')

    def _has_frame(self) -> bool:
        return (
            len(self._buffer) >= APP_FRAME_HEADER_SIZE
            and len(self._buffer) >= APP_FRAME_HEADER_SIZE + int.from_bytes(self._buffer[:2], 'big')
        )

    def receive(self) -> bytes:
        while (frame := pop_app_frame(self._buffer)) is None:
            data = self._client.recv(65535)
            if not data:
                raise ConnectionError('client closed the connection')
            self._buffer.extend(data)
        self._logger.debug('APP %s:%d C->S 0x%04x %s', self.host, self.port, frame_command(frame), frame.hex(' '))
        return frame

    def send(self, frame: bytes) -> None:
        self._logger.debug('APP %s:%d S->C 0x%04x %s', self.host, self.port, frame_command(frame), frame.hex(' '))
        self._client.sendall(frame)

    def reply(self, request: bytes, payload: bytes = b'') -> None:
        """Answer one client request with its command and sequence."""

        self.send(
            app_frame(APP_DIRECTION_REPLY, frame_command(request), sequence=frame_sequence(request), payload=payload)
        )

    def linger(self, seconds: float) -> None:
        """Give the client time to close first after the final reply."""

        self._client.settimeout(0.05)
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            try:
                if not self._client.recv(65535):
                    return
            except socket.timeout:
                continue
