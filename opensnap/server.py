"""UDP transport server for openSNAP."""

from collections import defaultdict
import logging
import socket
import time

from opensnap.config import ServerConfig, default_app_config
from opensnap.core.engine import SnapProtocolEngine
from opensnap.env_loader import load_env_file
from opensnap.logging_utils import configure_logging, format_hexdump
from opensnap.plugins import create_game_plugin
from opensnap.protocol.codec import encode_messages
from opensnap.protocol.models import Endpoint, SnapMessage


class SnapUdpServer:
    """Blocking UDP server with periodic tick processing."""

    def __init__(self, *, config: ServerConfig, engine: SnapProtocolEngine) -> None:
        self._config = config
        self._engine = engine
        self._stopped = False
        self._logger = logging.getLogger('opensnap.udp')

    def run(self) -> None:
        """Run UDP loop until stopped."""

        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp_socket:
            udp_socket.bind((self._config.host, self._config.port))
            self._logger.info(
                'UDP socket bound at %s:%d.',
                self._config.host,
                self._config.port,
            )
            next_tick = time.monotonic() + self._config.tick_interval_seconds

            while not self._stopped:
                timeout = max(0.0, next_tick - time.monotonic())
                udp_socket.settimeout(min(timeout, 0.5))

                try:
                    payload, (host, port) = udp_socket.recvfrom(4096)
                except socket.timeout:
                    payload = b''
                except OSError:
                    break

                if payload:
                    self._logger.info(
                        'Received datagram from %s:%d (%d byte(s)).',
                        host,
                        port,
                        len(payload),
                    )
                    self._logger.debug(
                        'Received hexdump from %s:%d\n%s',
                        host,
                        port,
                        format_hexdump(payload),
                    )
                    endpoint = Endpoint(host=host, port=port)
                    result = self._engine.handle_datagram(payload, endpoint)
                    self._send_messages(udp_socket, result.messages)
                    for error in result.errors:
                        self._logger.error('Engine error from %s:%d: %s', host, port, error)

                now = time.monotonic()
                if now >= next_tick:
                    tick_messages = self._engine.tick()
                    if tick_messages:
                        self._logger.debug(
                            'Tick produced %d outbound message(s).',
                            len(tick_messages),
                        )
                    self._send_messages(udp_socket, tick_messages)
                    next_tick = now + self._config.tick_interval_seconds

    def stop(self) -> None:
        """Request graceful loop stop."""

        self._stopped = True

    def _send_messages(self, udp_socket: socket.socket, messages: list[SnapMessage]) -> None:
        """Encode and send grouped outbound messages."""

        grouped: dict[Endpoint, list[SnapMessage]] = defaultdict(list)
        for message in messages:
            grouped[message.endpoint].append(message)

        for endpoint, endpoint_messages in grouped.items():
            datagram = encode_messages(endpoint_messages)
            commands = ', '.join(f'0x{message.command:02x}' for message in endpoint_messages)
            self._logger.info(
                'Sending datagram to %s:%d (%d byte(s), %d message(s)).',
                endpoint.host,
                endpoint.port,
                len(datagram),
                len(endpoint_messages),
            )
            self._logger.debug(
                'Outbound commands to %s:%d: %s.',
                endpoint.host,
                endpoint.port,
                commands,
            )
            self._logger.debug(
                'Outbound hexdump to %s:%d\n%s',
                endpoint.host,
                endpoint.port,
                format_hexdump(datagram),
            )
            udp_socket.sendto(datagram, (endpoint.host, endpoint.port))


def main() -> None:
    """CLI entrypoint."""

    load_env_file()
    configure_logging()
    logger = logging.getLogger('opensnap.udp')

    config = default_app_config()
    plugin = create_game_plugin(config.server.game_plugin)
    engine = SnapProtocolEngine(config=config, plugin=plugin)
    server = SnapUdpServer(config=config.server, engine=engine)
    logger.info(
        'openSNAP listening on %s:%d using plugin %s.',
        config.server.host,
        config.server.port,
        plugin.name,
    )
    try:
        server.run()
    except KeyboardInterrupt:
        logger.info('Received keyboard interrupt, shutting down UDP service.')


if __name__ == '__main__':
    main()
