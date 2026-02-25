"""UDP transport server for openSNAP."""

from collections import defaultdict
import socket
import time

from opensnap.config import ServerConfig, default_app_config
from opensnap.core.engine import SnapProtocolEngine
from opensnap.plugins import create_game_plugin
from opensnap.protocol.codec import encode_messages
from opensnap.protocol.models import Endpoint, SnapMessage


class SnapUdpServer:
    """Blocking UDP server with periodic tick processing."""

    def __init__(self, *, config: ServerConfig, engine: SnapProtocolEngine) -> None:
        self._config = config
        self._engine = engine
        self._stopped = False

    def run(self) -> None:
        """Run UDP loop until stopped."""

        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp_socket:
            udp_socket.bind((self._config.host, self._config.port))
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
                    endpoint = Endpoint(host=host, port=port)
                    result = self._engine.handle_datagram(payload, endpoint)
                    self._send_messages(udp_socket, result.messages)
                    for error in result.errors:
                        print(f'Engine error from {host}:{port}: {error}')

                now = time.monotonic()
                if now >= next_tick:
                    self._send_messages(udp_socket, self._engine.tick())
                    next_tick = now + self._config.tick_interval_seconds

    def stop(self) -> None:
        """Request graceful loop stop."""

        self._stopped = True

    @staticmethod
    def _send_messages(udp_socket: socket.socket, messages: list[SnapMessage]) -> None:
        """Encode and send grouped outbound messages."""

        grouped: dict[Endpoint, list[SnapMessage]] = defaultdict(list)
        for message in messages:
            grouped[message.endpoint].append(message)

        for endpoint, endpoint_messages in grouped.items():
            datagram = encode_messages(endpoint_messages)
            udp_socket.sendto(datagram, (endpoint.host, endpoint.port))


def main() -> None:
    """CLI entrypoint."""

    config = default_app_config()
    plugin = create_game_plugin(config.server.game_plugin)
    engine = SnapProtocolEngine(config=config, plugin=plugin)
    server = SnapUdpServer(config=config.server, engine=engine)
    print(
        f'openSNAP listening on {config.server.host}:{config.server.port} '
        f'using plugin {plugin.name}.'
    )
    try:
        server.run()
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
