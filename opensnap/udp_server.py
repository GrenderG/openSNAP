"""Shared UDP transport server for openSNAP services."""

from dataclasses import dataclass
import logging
import socket
import time

from opensnap.config import DEFAULT_TICK_INTERVAL_SECONDS, ServiceEndpointConfig
from opensnap.core.engine import SnapProtocolEngine
from opensnap.logging_utils import format_hexdump
from opensnap.protocol.codec import PacketDecodeError, detect_footer_bytes
from opensnap.protocol.constants import (
    FLAG_RESPONSE,
    FLAG_RELIABLE,
    FOOTER_BYTES,
)
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
    retry_cap_logged: bool = False


class SnapUdpServer:
    """Blocking UDP transport loop for SNAP game traffic.

    This class is the server-side approximation of the SN@P client's reliable
    UDP layer. The game protocol rides on plain UDP datagrams, but some SNAP
    messages are marked reliable and must stay in a resend queue until the peer
    explicitly acknowledges them.

    The important transport rules are:

    - One logical outbound `SnapMessage` is encoded into one UDP datagram
      unless the game/plugin has already built a synthetic multi-message SNAP
      payload.
    - Outbound reliable messages are tracked by
      `(endpoint, session_id, sequence_number)` before they are sent.
    - Reliable retirement is driven only by inbound response-path ACK state.
      That mirrors the client `kkDispatchingPacket -> kkSetRevAck` path:
      response packets carry the reverse ACK that retires one exact queued
      reliable sequence. Retirement is exact-sequence only, never cumulative.
    - Retransmission is oldest-pending-per-session. The server retries every
      `200 ms`, up to four counted retransmits, which matches the client
      `kkSendOperation` cadence and retry gate.
    - After the retry cap is reached, the packet stays pending while the peer
      is still sending traffic. This avoids disconnecting an active client just
      because one reliable packet was lost.
    - If the same peer stays inbound-silent for `5 s` after a reliable packet
      has already hit the retry cap, the server treats that as a transport
      timeout and asks the engine/plugin to clean up the timed-out session.
    - If the session is already gone by the time retransmit logic runs, the
      server immediately clears the stale pending state instead of retrying
      forever.

    The main loop therefore has four phases:

    1. Receive one datagram and remember the footer variant used by that peer.
    2. Retire any exact reliable ACKs carried by response packets.
    3. Dispatch the payload through the protocol engine and send fresh replies.
    4. Periodically tick the engine and retransmit due reliable packets.

    This docstring intentionally describes the transport contract in human
    terms because the protocol is easy to misunderstand: ordinary receive-side
    reliable acceptance and sender-side reverse-ACK retirement are separate
    mechanisms in the client, and the server has to preserve that distinction.
    """

    _RETRANSMIT_INTERVAL_SECONDS = 0.20
    _SESSION_INACTIVITY_TIMEOUT_SECONDS = 5.0
    _MAX_RETRANSMIT_ATTEMPTS = 4

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
        self._footer_bytes_by_endpoint: dict[tuple[str, int], bytes] = {}
        self._last_inbound_at_by_endpoint: dict[tuple[str, int], float] = {}

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
                        received_at = time.monotonic()
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
                        self._last_inbound_at_by_endpoint[(endpoint.host, endpoint.port)] = received_at
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
        """Encode and send outbound messages.

        Keep one outbound message per UDP datagram unless the protocol handler
        already built a synthetic multi-message SNAP payload itself.
        """

        send_time = time.monotonic()
        for message in messages:
            endpoint = message.endpoint
            datagram = self._engine.encode_messages(
                [message],
                footer_bytes=self._footer_bytes_by_endpoint.get((endpoint.host, endpoint.port), FOOTER_BYTES),
            )
            self._track_reliable(message, datagram, send_time)
            self._send_new_datagram(
                udp_socket,
                endpoint=endpoint,
                command=message.command,
                datagram=datagram,
            )

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
        """Retire pending reliable packets from response-path reverse ACKs."""

        try:
            messages = self._engine.decode_datagram(payload, endpoint)
        except PacketDecodeError:
            return

        for message in messages:
            if (message.type_flags & FLAG_RESPONSE) == 0:
                continue

            self._clear_reliable_pending(endpoint, message.session_id, message.acknowledge_number)

    def _track_reliable(
        self,
        message: SnapMessage,
        datagram: bytes,
        send_time: float,
    ) -> None:
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

    def _clear_reliable_pending(self, endpoint: Endpoint, session_id: int, acknowledge_number: int) -> None:
        """Clear one exactly acknowledged reliable packet for one peer/session."""

        keys_to_remove = [
            key
            for key in self._reliable_pending
            if key[0] == endpoint.host
            and key[1] == endpoint.port
            and key[2] == session_id
            and key[3] == acknowledge_number
        ]
        for key in keys_to_remove:
            self._reliable_pending.pop(key, None)

    def _retransmit_due(self, udp_socket: socket.socket) -> None:
        """Retransmit due reliable packets."""

        if not self._reliable_pending:
            return

        now = time.monotonic()

        for key in self._oldest_pending_keys_by_session():
            pending = self._reliable_pending.get(key)
            if pending is None:
                continue
            if self._drop_missing_session_pending(pending):
                continue
            if (now - pending.last_sent_at) < self._RETRANSMIT_INTERVAL_SECONDS:
                continue
            if pending.retransmit_attempts >= self._MAX_RETRANSMIT_ATTEMPTS:
                if not pending.retry_cap_logged:
                    pending.retry_cap_logged = True
                    self._logger.warning(
                        (
                            'Reliable packet 0x%02x to %s:%d '
                            '(sess=0x%08x seq=%d) reached retry cap; '
                            'keeping it pending while the peer is still active.'
                        ),
                        pending.command,
                        pending.endpoint.host,
                        pending.endpoint.port,
                        pending.session_id,
                        pending.sequence_number,
                    )
                if self._peer_is_inactive(pending.endpoint, now):
                    self._timeout_session(udp_socket, pending)
                    continue
                self._logger.debug(
                    (
                        'Retransmitting capped reliable packet 0x%02x to %s:%d '
                        '(sess=0x%08x seq=%d).'
                    ),
                    pending.command,
                    pending.endpoint.host,
                    pending.endpoint.port,
                    pending.session_id,
                    pending.sequence_number,
                )
                udp_socket.sendto(pending.datagram, (pending.endpoint.host, pending.endpoint.port))
                pending.last_sent_at = now
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

    def _oldest_pending_keys_by_session(self) -> list[tuple[str, int, int, int]]:
        """Return the oldest pending reliable packet per endpoint/session."""

        oldest_keys: list[tuple[str, int, int, int]] = []
        seen_sessions: set[tuple[str, int, int]] = set()
        for key in sorted(self._reliable_pending):
            session_key = key[:3]
            if session_key in seen_sessions:
                continue
            seen_sessions.add(session_key)
            oldest_keys.append(key)
        return oldest_keys

    def _drop_missing_session_pending(self, pending: _ReliablePending) -> bool:
        """Drop reliable transport state for sessions that no longer exist."""

        session = self._engine.resolve_session(pending.endpoint, pending.session_id)
        if session is None:
            self._clear_session_pending(pending.endpoint, pending.session_id)
            self._clear_session_runtime(pending.endpoint)
            return True

        return False

    def _peer_is_inactive(self, endpoint: Endpoint, now: float) -> bool:
        """Check whether one endpoint has been inbound-silent long enough to time out."""

        last_inbound_at = self._last_inbound_at_by_endpoint.get((endpoint.host, endpoint.port))
        if last_inbound_at is None:
            return False
        return (now - last_inbound_at) >= self._SESSION_INACTIVITY_TIMEOUT_SECONDS

    def _clear_session_pending(
        self,
        endpoint: Endpoint,
        session_id: int,
    ) -> None:
        """Remove pending reliable packets for one endpoint/session."""

        session_keys = [
            key
            for key, pending in self._reliable_pending.items()
            if key[0] == endpoint.host
            and key[1] == endpoint.port
            and key[2] == session_id
        ]
        for key in session_keys:
            self._reliable_pending.pop(key, None)

    def _clear_session_runtime(self, endpoint: Endpoint) -> None:
        """Drop cached transport-side state for one endpoint."""

        self._footer_bytes_by_endpoint.pop((endpoint.host, endpoint.port), None)
        self._last_inbound_at_by_endpoint.pop((endpoint.host, endpoint.port), None)

    def _timeout_session(self, udp_socket: socket.socket, pending: _ReliablePending) -> None:
        """Fail one peer session after reliable retransmits outlive inbound activity."""

        now = time.monotonic()
        self._logger.warning(
            (
                'Reliable session timeout after packet 0x%02x to %s:%d '
                '(sess=0x%08x seq=%d) while peer stayed inactive for %.1f second(s).'
            ),
            pending.command,
            pending.endpoint.host,
            pending.endpoint.port,
            pending.session_id,
            pending.sequence_number,
            now - self._last_inbound_at_by_endpoint.get((pending.endpoint.host, pending.endpoint.port), now),
        )

        self._clear_session_pending(pending.endpoint, pending.session_id)
        self._clear_session_runtime(pending.endpoint)

        cleanup_messages = self._engine.handle_transport_timeout(pending.endpoint, pending.session_id)
        if cleanup_messages:
            self._send_messages(udp_socket, cleanup_messages)

    def _send_new_datagram(
        self,
        udp_socket: socket.socket,
        *,
        endpoint: Endpoint,
        command: int,
        datagram: bytes,
    ) -> None:
        """Log and send one freshly produced outbound datagram."""

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
            command,
        )
        self._logger.debug(
            'Outbound hexdump to %s:%d\n%s',
            endpoint.host,
            endpoint.port,
            format_hexdump(datagram),
        )
        udp_socket.sendto(datagram, (endpoint.host, endpoint.port))
