"""Shared UDP transport server for openSNAP services."""

from dataclasses import dataclass
import logging
import socket
import time

from opensnap.config import DEFAULT_TICK_INTERVAL_SECONDS, ServiceEndpointConfig
from opensnap.core.engine import SnapProtocolEngine
from opensnap.logging_utils import format_hexdump
from opensnap.protocol import commands
from opensnap.protocol.codec import PacketDecodeError, decode_datagram, detect_footer_bytes, encode_messages
from opensnap.protocol.constants import BARE_ACK_FLAGS, FLAG_RELIABLE, FOOTER_BYTES
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

    def __init__(
        self,
        *,
        config: ServiceEndpointConfig,
        engine: SnapProtocolEngine,
        tick_interval_seconds: float = DEFAULT_TICK_INTERVAL_SECONDS,
        logger_name: str = 'opensnap.game',
    ) -> None:
        self._config = config
        self._engine = engine
        self._tick_interval_seconds = tick_interval_seconds
        self._stopped = False
        self._logger = logging.getLogger(logger_name)
        self._reliable_pending: dict[tuple[str, int, int, int], _ReliablePending] = {}
        self._last_sent_sequence: dict[tuple[str, int, int], int] = {}
        self._footer_bytes_by_endpoint: dict[tuple[str, int], bytes] = {}

    def run(self) -> None:
        """Run UDP loop until stopped."""

        try:
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
                next_tick = time.monotonic() + self._tick_interval_seconds

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
                        self._remember_footer_variant(payload, endpoint)
                        self._process_transport_acks(payload, endpoint)
                        result = self._engine.handle_datagram(payload, endpoint)
                        self._send_messages(udp_socket, result.messages)
                        self._log_engine_errors(endpoint, payload, result.errors)

                    now = time.monotonic()
                    if now >= next_tick:
                        tick_messages = self._engine.tick()
                        if tick_messages:
                            self._logger.debug(
                                'Tick produced %d outbound message(s).',
                                len(tick_messages),
                            )
                        self._send_messages(udp_socket, tick_messages)
                        next_tick = now + self._tick_interval_seconds

                    self._retransmit_due(udp_socket)
        finally:
            self._engine.close()

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
        """Encode and send outbound messages."""

        send_time = time.monotonic()
        for message in messages:
            endpoint = message.endpoint
            datagram = encode_messages(
                [message],
                footer_bytes=self._footer_bytes_by_endpoint.get((endpoint.host, endpoint.port), FOOTER_BYTES),
            )
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

    def _remember_footer_variant(self, payload: bytes, endpoint: Endpoint) -> None:
        """Track which supported SNAP footer marker one endpoint is using."""

        try:
            footer_bytes = detect_footer_bytes(payload)
        except PacketDecodeError:
            return

        self._footer_bytes_by_endpoint[(endpoint.host, endpoint.port)] = footer_bytes

    def _log_engine_errors(self, endpoint: Endpoint, payload: bytes, errors: list[str]) -> None:
        """Log engine errors with a visible inbound hexdump for debugging."""

        if not errors:
            return

        self._logger.error(
            'Inbound hexdump for engine error from %s:%d\n%s',
            endpoint.host,
            endpoint.port,
            format_hexdump(payload),
        )
        for error in errors:
            self._logger.error('Engine error from %s:%d: %s', endpoint.host, endpoint.port, error)

    def _process_transport_acks(self, payload: bytes, endpoint: Endpoint) -> None:
        """Track bare ACK frames and clear pending reliable packets."""

        try:
            messages = decode_datagram(payload, endpoint)
        except PacketDecodeError:
            return

        for message in messages:
            is_bare_ack = (
                message.command == commands.CMD_ACK
                and (message.type_flags & BARE_ACK_FLAGS) == BARE_ACK_FLAGS
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

        self._prune_reliable_queue_if_needed(message.endpoint, message.session_id)

    def _prune_reliable_queue_if_needed(self, endpoint: Endpoint, session_id: int) -> None:
        """Cap pending reliable packets per session by dropping the oldest entries."""

        session_keys = [
            key
            for key in self._reliable_pending
            if key[0] == endpoint.host and key[1] == endpoint.port and key[2] == session_id
        ]
        overflow = len(session_keys) - self._MAX_PENDING_RELIABLE_PER_SESSION
        if overflow <= 0:
            return

        session_keys.sort(key=lambda item: item[3])
        dropped = session_keys[:overflow]
        dropped_sequences = [key[3] for key in dropped]
        for key in dropped:
            self._reliable_pending.pop(key, None)

        self._logger.warning(
            (
                'Reliable queue pressure for %s:%d (sess=0x%08x): '
                'dropped %d oldest packet(s), seq %d..%d.'
            ),
            endpoint.host,
            endpoint.port,
            session_id,
            overflow,
            min(dropped_sequences),
            max(dropped_sequences),
        )

    def _normalize_transport_ack(
        self,
        endpoint: Endpoint,
        session_id: int,
        acknowledge_number: int,
    ) -> int | None:
        """Validate one transport ACK against the last sequence sent to that peer."""

        last_sent = self._last_sent_sequence.get((endpoint.host, endpoint.port, session_id))
        if last_sent is None:
            return acknowledge_number

        upper_bound = last_sent + self._ACK_FUTURE_SLACK
        if acknowledge_number <= upper_bound:
            return acknowledge_number

        swapped = int.from_bytes(
            acknowledge_number.to_bytes(4, byteorder='big', signed=False),
            byteorder='little',
            signed=False,
        )
        if swapped <= upper_bound:
            return swapped

        return None

    def _clear_reliable_pending(self, endpoint: Endpoint, session_id: int, acknowledge_number: int) -> None:
        """Clear all reliable packets cumulatively acknowledged by one peer."""

        keys_to_remove = [
            key
            for key in self._reliable_pending
            if key[0] == endpoint.host
            and key[1] == endpoint.port
            and key[2] == session_id
            and key[3] <= acknowledge_number
        ]
        for key in keys_to_remove:
            self._reliable_pending.pop(key, None)

    def _note_sent_sequence(self, message: SnapMessage) -> None:
        """Remember the highest outbound sequence per endpoint/session."""

        if (message.type_flags & FLAG_RELIABLE) == 0:
            return

        key = (message.endpoint.host, message.endpoint.port, message.session_id)
        current = self._last_sent_sequence.get(key)
        if current is None or message.sequence_number > current:
            self._last_sent_sequence[key] = message.sequence_number

    def _retransmit_due(self, udp_socket: socket.socket) -> None:
        """Retransmit due reliable packets and expire exhausted entries."""

        if not self._reliable_pending:
            return

        now = time.monotonic()
        retransmit_count = 0
        deferred_due_packets = False

        for key in sorted(self._reliable_pending):
            pending = self._reliable_pending.get(key)
            if pending is None:
                continue
            if (now - pending.last_sent_at) < self._RETRANSMIT_INTERVAL_SECONDS:
                continue
            if retransmit_count >= self._MAX_RETRANSMITS_PER_CYCLE:
                deferred_due_packets = True
                continue
            if pending.retransmit_attempts >= self._MAX_RETRANSMIT_ATTEMPTS:
                self._logger.warning(
                    (
                        'Dropping reliable packet 0x%02x to %s:%d '
                        '(sess=0x%08x seq=%d) after %d retry attempt(s).'
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
