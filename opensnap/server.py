"""UDP transport server for openSNAP."""
from dataclasses import dataclass
import logging
import socket
import time

from opensnap.config import ServerConfig, default_app_config
from opensnap.core.engine import SnapProtocolEngine
from opensnap.env_loader import load_env_file
from opensnap.logging_utils import configure_logging, format_hexdump
from opensnap.plugins import create_game_plugin
from opensnap.protocol import commands
from opensnap.protocol.codec import PacketDecodeError, decode_datagram, encode_messages
from opensnap.protocol.constants import FLAG_RELIABLE
from opensnap.protocol.models import Endpoint, SnapMessage


@dataclass(slots=True)
class _ReliablePending:
    """Reliable packet waiting for client ACK."""

    endpoint: Endpoint
    session_id: int
    sequence_number: int
    command: int
    datagram: bytes
    last_sent_at: float
    retransmit_attempts: int = 0


class SnapUdpServer:
    """Blocking UDP server with periodic tick processing."""

    _RETRANSMIT_INTERVAL_SECONDS = 0.25
    _MAX_RETRANSMIT_ATTEMPTS = 4
    _MAX_RETRANSMITS_PER_CYCLE = 128
    _MAX_PENDING_RELIABLE_PER_SESSION = 256
    _ACK_FUTURE_SLACK = 512

    def __init__(self, *, config: ServerConfig, engine: SnapProtocolEngine) -> None:
        self._config = config
        self._engine = engine
        self._stopped = False
        self._logger = logging.getLogger('opensnap.udp')
        self._reliable_pending: dict[tuple[str, int, int, int], _ReliablePending] = {}
        self._last_sent_sequence: dict[tuple[str, int, int], int] = {}

    def run(self) -> None:
        """Run UDP loop until stopped."""

        try:
            udp_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        except OSError as exc:
            self._logger.error('Failed to create UDP socket: %s', exc)
            raise

        with udp_socket:
            self._enable_reuse_address(udp_socket)
            try:
                udp_socket.bind((self._config.host, self._config.port))
            except OSError as exc:
                self._logger.error(
                    'Failed to bind UDP socket on %s:%d: %s',
                    self._config.host,
                    self._config.port,
                    exc,
                )
                raise
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
                    self._process_transport_acks(payload, endpoint)
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

                self._retransmit_due(udp_socket)

    def _enable_reuse_address(self, udp_socket: socket.socket) -> None:
        """Enable address reuse to reduce restart/bind failures across platforms."""

        try:
            udp_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        except OSError as exc:
            self._logger.warning('Failed to enable SO_REUSEADDR on UDP socket: %s', exc)

    def stop(self) -> None:
        """Request graceful loop stop."""

        self._stopped = True

    def _send_messages(self, udp_socket: socket.socket, messages: list[SnapMessage]) -> None:
        """Encode and send outbound messages.

        Send each SNAP message as one UDP datagram. Some clients appear to parse only
        one top-level SNAP entry per UDP packet for certain flows (notably room-exit),
        so bundling multiple responses into one datagram can cause retries/timeouts.
        """

        send_time = time.monotonic()
        for message in messages:
            datagram = encode_messages([message])
            endpoint = message.endpoint
            self._logger.info(
                'Sending datagram to %s:%d (%d byte(s), %d message(s)).',
                endpoint.host,
                endpoint.port,
                len(datagram),
                1,
            )
            self._logger.debug(
                'Outbound commands to %s:%d: 0x%02x.',
                endpoint.host,
                endpoint.port,
                message.command,
            )
            self._logger.debug(
                'Outbound hexdump to %s:%d\n%s',
                endpoint.host,
                endpoint.port,
                format_hexdump(datagram),
            )
            udp_socket.sendto(datagram, (endpoint.host, endpoint.port))
            self._note_sent_sequence(message)
            self._track_reliable(message, datagram, send_time)

    def _process_transport_acks(self, payload: bytes, endpoint: Endpoint) -> None:
        """Track bare ACK frames and clear pending reliable packets."""

        try:
            messages = decode_datagram(payload, endpoint)
        except PacketDecodeError:
            return

        for message in messages:
            is_bare_ack = (
                message.command == commands.CMD_ACK
                and (message.type_flags & 0x6000) == 0x6000
            )
            is_reliable_with_piggyback_ack = (message.type_flags & FLAG_RELIABLE) != 0

            if not is_bare_ack and not is_reliable_with_piggyback_ack:
                continue

            acknowledge_number = self._normalize_transport_ack(
                endpoint,
                message.session_id,
                message.acknowledge_number,
            )
            if acknowledge_number is None:
                self._logger.debug(
                    (
                        'Ignoring implausible transport ACK from %s:%d '
                        '(sess=0x%08x ack=%d).'
                    ),
                    endpoint.host,
                    endpoint.port,
                    message.session_id,
                    message.acknowledge_number,
                )
                continue

            self._clear_reliable_pending(endpoint, message.session_id, acknowledge_number)

    def _track_reliable(self, message: SnapMessage, datagram: bytes, send_time: float) -> None:
        """Store outgoing reliable packets until acknowledged."""

        if (message.type_flags & FLAG_RELIABLE) == 0:
            return

        key = (
            message.endpoint.host,
            message.endpoint.port,
            message.session_id,
            message.sequence_number,
        )
        self._reliable_pending[key] = _ReliablePending(
            endpoint=message.endpoint,
            session_id=message.session_id,
            sequence_number=message.sequence_number,
            command=message.command,
            datagram=datagram,
            last_sent_at=send_time,
        )
        self._enforce_pending_limit(message.endpoint, message.session_id)

    def _note_sent_sequence(self, message: SnapMessage) -> None:
        """Record highest outbound sequence per endpoint/session for ACK sanity checks."""

        key = (message.endpoint.host, message.endpoint.port, message.session_id)
        previous = self._last_sent_sequence.get(key)
        if previous is None or message.sequence_number > previous:
            self._last_sent_sequence[key] = message.sequence_number

    def _is_plausible_ack(self, endpoint: Endpoint, session_id: int, acknowledge_number: int) -> bool:
        """Validate ACK numbers before they mutate reliable pending state."""

        key = (endpoint.host, endpoint.port, session_id)
        max_known_sequence = self._last_sent_sequence.get(key)
        if max_known_sequence is None:
            max_known_sequence = self._max_pending_sequence(endpoint, session_id)
            if max_known_sequence is None:
                return False

        return acknowledge_number <= (max_known_sequence + self._ACK_FUTURE_SLACK)

    def _normalize_transport_ack(
        self,
        endpoint: Endpoint,
        session_id: int,
        acknowledge_number: int,
    ) -> int | None:
        """Return a plausible transport ACK value for one endpoint/session.

        Some game-end client packets carry a byte-swapped ACK field. Accept that
        variant only when the swapped value is plausible for current reliability
        state, otherwise keep strict rejection.
        """

        if self._is_plausible_ack(endpoint, session_id, acknowledge_number):
            return acknowledge_number

        swapped_ack = self._swap_u32(acknowledge_number)
        if swapped_ack != acknowledge_number and self._is_plausible_ack(endpoint, session_id, swapped_ack):
            self._logger.debug(
                (
                    'Using byte-swapped transport ACK from %s:%d '
                    '(sess=0x%08x raw=%d swapped=%d).'
                ),
                endpoint.host,
                endpoint.port,
                session_id,
                acknowledge_number,
                swapped_ack,
            )
            return swapped_ack

        return None

    @staticmethod
    def _swap_u32(value: int) -> int:
        """Swap endianness for one 32-bit unsigned integer."""

        return (
            ((value & 0x000000FF) << 24)
            | ((value & 0x0000FF00) << 8)
            | ((value & 0x00FF0000) >> 8)
            | ((value & 0xFF000000) >> 24)
        )

    def _max_pending_sequence(self, endpoint: Endpoint, session_id: int) -> int | None:
        """Return highest tracked reliable sequence for one endpoint/session."""

        max_sequence: int | None = None
        for host, port, pending_session_id, pending_sequence in self._reliable_pending:
            if host != endpoint.host or port != endpoint.port:
                continue
            if pending_session_id != session_id:
                continue
            if max_sequence is None or pending_sequence > max_sequence:
                max_sequence = pending_sequence
        return max_sequence

    def _enforce_pending_limit(self, endpoint: Endpoint, session_id: int) -> None:
        """Bound per-session reliable backlog to avoid transport collapse under loss."""

        session_keys = [
            key
            for key in self._reliable_pending
            if key[0] == endpoint.host and key[1] == endpoint.port and key[2] == session_id
        ]
        overflow = len(session_keys) - self._MAX_PENDING_RELIABLE_PER_SESSION
        if overflow <= 0:
            return

        session_keys.sort(key=lambda item: item[3])
        dropped_sequences: list[int] = []
        for key in session_keys[:overflow]:
            pending = self._reliable_pending.pop(key, None)
            if pending is not None:
                dropped_sequences.append(pending.sequence_number)

        if dropped_sequences:
            self._logger.warning(
                (
                    'Reliable queue pressure for %s:%d (sess=0x%08x): '
                    'dropped %d oldest packet(s), seq %d..%d.'
                ),
                endpoint.host,
                endpoint.port,
                session_id,
                len(dropped_sequences),
                dropped_sequences[0],
                dropped_sequences[-1],
            )

    def _clear_reliable_pending(self, endpoint: Endpoint, session_id: int, acknowledge_number: int) -> None:
        """Drop reliable packets that are acknowledged by one client session."""

        for key in list(self._reliable_pending):
            host, port, pending_session_id, pending_sequence = key
            if host != endpoint.host or port != endpoint.port:
                continue
            if pending_session_id != session_id:
                continue
            if pending_sequence <= acknowledge_number:
                self._reliable_pending.pop(key, None)

    def _retransmit_due(self, udp_socket: socket.socket) -> None:
        """Retransmit due reliable packets when ACKs are missing."""

        now = time.monotonic()
        due_items = [
            (key, pending)
            for key, pending in self._reliable_pending.items()
            if now - pending.last_sent_at >= self._RETRANSMIT_INTERVAL_SECONDS
        ]
        due_items.sort(key=lambda item: item[1].last_sent_at)

        retransmit_count = 0
        deferred_due_packets = False
        for key, pending in due_items:
            if pending.retransmit_attempts >= self._MAX_RETRANSMIT_ATTEMPTS:
                self._logger.warning(
                    (
                        'Dropping reliable packet 0x%02x to %s:%d '
                        '(sess=0x%08x seq=%d) after %d retries without ACK.'
                    ),
                    pending.command,
                    pending.endpoint.host,
                    pending.endpoint.port,
                    pending.session_id,
                    pending.sequence_number,
                    pending.retransmit_attempts,
                )
                self._reliable_pending.pop(key, None)
                continue

            if retransmit_count >= self._MAX_RETRANSMITS_PER_CYCLE:
                deferred_due_packets = True
                continue

            self._logger.debug(
                (
                    'Retransmitting reliable packet 0x%02x to %s:%d '
                    '(sess=0x%08x seq=%d attempt=%d).'
                ),
                pending.command,
                pending.endpoint.host,
                pending.endpoint.port,
                pending.session_id,
                pending.sequence_number,
                pending.retransmit_attempts + 1,
            )
            udp_socket.sendto(pending.datagram, (pending.endpoint.host, pending.endpoint.port))
            pending.retransmit_attempts += 1
            pending.last_sent_at = now
            retransmit_count += 1

        if deferred_due_packets:
            self._logger.debug(
                (
                    'Retransmit budget exhausted (%d packet(s) this cycle); '
                    'deferring remaining due reliable packets.'
                ),
                self._MAX_RETRANSMITS_PER_CYCLE,
            )


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
        'Starting openSNAP UDP on %s:%d using plugin %s.',
        config.server.host,
        config.server.port,
        plugin.name,
    )
    try:
        server.run()
    except KeyboardInterrupt:
        logger.info('Received keyboard interrupt, shutting down UDP service.')
    except OSError:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
