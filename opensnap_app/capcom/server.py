"""Capcom APP TCP server: one listener for every Capcom title.

Every title connects to the same address and waits for the server's 1002. The
client's answer carries its build byte (`ConnectAnswer`), which tells the titles
apart, so the server opens each connection itself and then hands it to the
service of the title that owns that build. A pre-release build that shares a
title's build byte but needs its own service (its own players and data) is
recognised by its build stamp instead.
"""

from collections.abc import Mapping
import logging
import socket
import threading
from typing import Protocol

from opensnap_app.capcom.connection import AppConnection
from opensnap_app.capcom.protocol import (
    APP_DIRECTION_SERVER,
    CMD_APP_CONNECT,
    CONNECT_NO_SALT,
    AppCodecError,
    ConnectAnswer,
    app_frame,
    read_connect_answer,
)

_LOGGER = logging.getLogger('opensnap_app.capcom')

# Every title answers the 1002 at once; the title services set their own timeouts after it.
OPENING_TIMEOUT_SECONDS = 30.0


class TitleService(Protocol):
    """One title's APP logic: everything after the 1002 exchange, until the connection is done."""

    def serve(self, connection: AppConnection, answer: ConnectAnswer) -> None: ...


class CapcomAppServer:
    """Accept APP clients, one thread each, and route them by client build."""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        titles: Mapping[int, TitleService],
        stamped_builds: Mapping[bytes, TitleService] | None = None,
    ) -> None:
        self._host = host
        self._port = port
        self._titles = dict(titles)
        # Build stamp (`ConnectAnswer.version`) -> service, ahead of the build byte.
        self._stamped_builds = dict(stamped_builds or {})
        self._listener: socket.socket | None = None
        self._stopped = threading.Event()

    def start(self) -> None:
        """Bind now so bind errors surface at startup, then accept in the background."""

        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            listener.bind((self._host, self._port))
            listener.listen(16)
        except OSError:
            listener.close()
            raise
        listener.settimeout(0.25)
        self._listener = listener
        self._accepting = threading.Thread(target=self._accept_loop, name='capcom-app', daemon=True)
        self._accepting.start()
        _LOGGER.info('Capcom APP listening on %s:%d.', self._host, self._port)

    def serve_forever(self) -> None:
        self.start()
        self._accepting.join()

    def stop(self) -> None:
        self._stopped.set()

    @property
    def address(self) -> tuple[str, int]:
        """Bound listener address (useful when the configured port is 0)."""

        assert self._listener is not None, 'start() binds the listener'
        return self._listener.getsockname()

    def _accept_loop(self) -> None:
        assert self._listener is not None
        with self._listener:
            while not self._stopped.is_set():
                try:
                    client, address = self._listener.accept()
                except socket.timeout:
                    continue
                except OSError:
                    _LOGGER.exception('APP accept failed; continuing.')
                    continue
                threading.Thread(target=self._serve, args=(client, address), daemon=True).start()

    def _serve(self, client: socket.socket, address: tuple[str, int]) -> None:
        with client:
            connection = AppConnection(client, address, _LOGGER)
            connection.set_timeout(OPENING_TIMEOUT_SECONDS)
            try:
                connection.send(app_frame(APP_DIRECTION_SERVER, CMD_APP_CONNECT, payload=CONNECT_NO_SALT))
                answer = read_connect_answer(connection.receive())
            except (ConnectionError, socket.timeout, AppCodecError) as exc:
                _LOGGER.info('APP connection from %s:%d closed before it was opened: %s', *address, exc)
                return
            title = self._stamped_builds.get(answer.version.rstrip(b'\x00')) or self._titles.get(answer.build)
            if title is None:
                _LOGGER.warning('APP connection from %s:%d has unknown client build %d.', *address, answer.build)
                return
            try:
                title.serve(connection, answer)
            except Exception:  # noqa: BLE001
                _LOGGER.exception('APP connection from %s:%d failed.', *address)
