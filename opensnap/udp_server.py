"""Shared UDP transport server for openSNAP services."""

from collections import deque
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
    type_flags: int
    command: int
    datagram: bytes
    last_sent_at: float
    retransmit_attempts: int = 0
    first_sent_at: float = 0.0


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
    - Reliable retirement uses the exact ACK number carried by inbound
      response packets, including bare ACK frames. That mirrors
      `kkDispatchingPacket -> kkSetRevAck` in the client.
    - Standalone transport ACKs must stay in that bare shape: `CMD_ACK`
      replies with no payload use `packet_number = 0` and `sequence_number = 0`.
      Both clients build ACK-only replies that way in `kkSendToTargetServer`,
      and reusing a fresh server sequence there can leave the client's own
      reliable send queue unretired.
    - The normal reliable receive path (`kkReceiveExtentCheck* ->
      kkSetAckNumber`) is separate: it queues an ACK for the received reliable
      sequence from packet offset `+8`, not from the incoming packet's own
      reverse-ACK field. Plain reliable `0xa000` room traffic is therefore not
      a valid reverse-ACK retirement source.
    - Embedded multi children still decode a trailing 32-bit header word, but
      it is not a second transport ACK stream. The client assigns ACK state
      from the outer reliable wrapper before `kkRUDPTopMultiMessageHandle`
      dispatches children, and the multi-child `kkSetMessage` path leaves the
      child `+0x0c` word uninitialized. In practice that inner `ack` field is
      just raw copied/reused header bytes.
    - Retransmission is oldest-pending-per-session. The server retries every
      `200 ms`, up to four counted retransmits, which matches the client
      `kkSendOperation` cadence and retry gate.
    - The peer reliable receive window is only 16 packets wide, so the server
      keeps at most 16 reliable packets in flight per endpoint/session and
      defers newer reliable work behind that window instead of emitting it
      immediately.
    - After the retry cap is reached, the packet stays pending but is no
      longer retransmitted. `kkSendOperation` stops requeueing at that point;
      the later watchdog is a timeout path, not a second resend loop.
    - If the same peer then stays inbound-silent for `60 s` after a reliable
      packet has already hit the retry cap, the server treats that as a
      transport timeout and asks the engine/plugin to clean up the timed-out
      session.
    - If the session is already gone by the time retransmit logic runs, the
      server immediately clears the stale pending state instead of retrying
      forever.

    The main loop therefore has four phases:

    1. Receive one datagram and remember the footer variant used by that peer.
    2. Retire any exact reliable ACKs carried by response packets.
    3. Dispatch the payload through the protocol engine and send fresh replies.
    4. Periodically tick the engine and retransmit due reliable packets.

    The split matters: the client handles reliable receive progression and
    reverse-ACK retirement in different paths, and the server has to keep that
    separation instead of collapsing both into one ACK rule.
    """

    # Every unacknowledged reliable packet is resent on its own timer, timed
    # like the MH-family sender (MH NA `slus_208.96`; the same code in MH EU and
    # Outbreak F1/F2), which walks every queued item (`0x001fd250`): resend once
    # `last send + RTT estimate + (len + 131) / 100 ms + backoff` has passed
    # (`0x001fd2e8..0x001fd328`, send stamp `0x001fe594..0x001fe5f0`, line speed
    # 100), the backoff starting at 17 ms (`0x00209724`) and growing by 200 ms
    # per resend (`0x001fd3c0..0x001fd3d8`). The estimate starts at the games'
    # INITIAL_LATENCY 1600 (`0x003760b0`) and is replaced by each ACK's sample
    # (`0x00212e78`). A copy sent before the ACK could come back is delivered
    # twice by these clients when the original waits behind a gap (no dedup:
    # append `0x002131a8`, consumer `0x00209a00`), so the first resend must
    # follow the measured round trip. Every copy is ACKed (MH `0x001fe83c`,
    # AM `0x002f4e78`), so resending until the ACK gets through is safe.
    _INITIAL_RTT_ESTIMATE_SECONDS = 1.6
    _INITIAL_BACKOFF_SECONDS = 0.017
    _BACKOFF_STEP_SECONDS = 0.20
    _RTT_ESTIMATE_LIMITS_SECONDS = (0.017, 32.0)
    # Upper bound on one socket wait while reliable packets are pending.
    _RETRANSMIT_INTERVAL_SECONDS = 0.20
    # Idle socket poll fallback. When no reliable packet is pending, the recv
    # loop can sleep longer without smearing the binary-backed `200 ms`
    # retransmit cadence; `_poll_timeout_seconds(...)` shortens this as soon as
    # one pending reliable has an earlier due time.
    _IDLE_SOCKET_POLL_SECONDS = 0.50
    # `kkCreateARUDPRevWindow(0x10)` allocates a 16-entry reliable receive
    # window in both builds:
    # - `SLUS_204.98` `0x002e6248`
    # - `SLUS_206.42` `0x002e98cc`
    # openSNAP uses the same width as its per-session in-flight reliable cap so
    # a missing oldest packet cannot be buried under more than the peer can
    # buffer behind that gap.
    _MAX_INFLIGHT_RELIABLE_PER_SESSION = 0x10
    # `kkReceiveData` refreshes the last-receive clock on every inbound packet,
    # and `kkDispatchOperationByPreriod` escalates to timeout after roughly
    # `0x1388` (`5000 ms`) of silence in both builds:
    # - `SLUS_204.98` `0x002e887c`, `0x002e8700..0x002e8778`
    # - `SLUS_206.42` `0x002eed94`, `0x002eeb14..0x002eebd8`
    # openSNAP intentionally uses a broader `60 s` server-side grace so clients
    # have more room to recover from temporary network hiccups before the
    # session is torn down.
    #_SESSION_INACTIVITY_TIMEOUT_SECONDS = 5.0
    _SESSION_INACTIVITY_TIMEOUT_SECONDS = 60.0

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
        self._deferred_reliable: dict[tuple[str, int, int], deque[SnapMessage]] = {}
        self._footer_bytes_by_endpoint: dict[tuple[str, int], bytes] = {}
        self._last_inbound_at_by_endpoint: dict[tuple[str, int], float] = {}
        self._rtt_estimate_by_endpoint: dict[tuple[str, int], float] = {}

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
                    now = time.monotonic()
                    udp_socket.settimeout(self._poll_timeout_seconds(now, next_tick))

                    try:
                        payload, (host, port) = udp_socket.recvfrom(4096)
                    except (socket.timeout, BlockingIOError):
                        # A zero poll timeout makes the socket non-blocking.
                        payload = b''
                    except OSError as exc:
                        self._logger.exception(
                            'UDP recvfrom failed on %s:%d; continuing: %s',
                            self._config.host,
                            self._config.port,
                            exc,
                        )
                        payload = b''

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
                        for old_endpoint, new_endpoint, moved_session_id in result.endpoint_moves:
                            self._move_session_transport(old_endpoint, new_endpoint, moved_session_id)
                        for _, restarted_session_id in result.reliable_restarts:
                            self._forget_session_reliables(restarted_session_id)
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

    def _poll_timeout_seconds(self, now: float, next_tick: float) -> float:
        """Return how long the recv loop may sleep before the next due task."""

        tick_delay = max(0.0, next_tick - now)
        retransmit_delay = self._next_retransmit_delay_seconds(now)
        if retransmit_delay is None:
            return min(tick_delay, self._IDLE_SOCKET_POLL_SECONDS)
        return min(tick_delay, retransmit_delay, self._RETRANSMIT_INTERVAL_SECONDS)

    def _next_retransmit_delay_seconds(self, now: float) -> float | None:
        """Return seconds until the next pending reliable becomes due."""

        next_delay: float | None = None
        for pending in self._reliable_pending.values():
            delay = max(0.0, self._retransmit_wait(pending) - (now - pending.last_sent_at))
            if next_delay is None or delay < next_delay:
                next_delay = delay
        return next_delay

    def _retransmit_wait(self, pending: _ReliablePending) -> float:
        """Return how long one pending reliable waits after its last send."""

        return self._rtt_estimate(pending.endpoint) + (len(pending.datagram) + 131) // 100 / 1000 + self._backoff(pending)

    def _backoff(self, pending: _ReliablePending) -> float:
        return self._INITIAL_BACKOFF_SECONDS + self._BACKOFF_STEP_SECONDS * pending.retransmit_attempts

    def _rtt_estimate(self, endpoint: Endpoint) -> float:
        return self._rtt_estimate_by_endpoint.get((endpoint.host, endpoint.port), self._INITIAL_RTT_ESTIMATE_SECONDS)

    def _sample_rtt(self, pending: _ReliablePending, now: float) -> None:
        """Replace the peer's RTT estimate with this ACK's sample, as the MH sender does (`0x00212e78`).

        A resent packet's sample adds the waits already spent on it
        (`0x00209800..0x00209834`); the sample is clamped to 17 ms..32 s, and
        one 11 times the old estimate or more becomes ten times it
        (`0x00212f04..0x00212f78`).
        """

        old = self._rtt_estimate(pending.endpoint)
        sample = now - pending.last_sent_at
        if pending.retransmit_attempts:
            sample += pending.retransmit_attempts * old + self._backoff(pending)
        low, high = self._RTT_ESTIMATE_LIMITS_SECONDS
        sample = min(max(sample, low), high)
        if sample >= old * 11:
            sample = old * 10
        self._rtt_estimate_by_endpoint[(pending.endpoint.host, pending.endpoint.port)] = sample

    def _send_messages(self, udp_socket: socket.socket, messages: list[SnapMessage]) -> None:
        """Encode and send outbound messages.

        Keep one outbound message per UDP datagram unless the protocol handler
        already built a synthetic multi-message SNAP payload itself.
        """

        send_time = time.monotonic()
        self._flush_deferred_reliable(udp_socket, send_time)
        for message in messages:
            if self._should_defer_reliable(message):
                self._enqueue_deferred_reliable(message)
                continue
            self._send_encoded_message(udp_socket, message, send_time)
        self._flush_deferred_reliable(udp_socket, send_time)

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
        """Retire pending reliable packets from exact ACKs on response packets."""

        try:
            messages = self._engine.decode_datagram(payload, endpoint)
        except PacketDecodeError:
            return

        for message in messages:
            if message.embedded_in_multi:
                continue
            if (message.type_flags & FLAG_RESPONSE) == 0:
                continue
            # Until the KICS answer the client's header carries its own word,
            # `(local IP << 16) | port` (AM `0x002e9bc0`, MH NA `0x001fc958`),
            # not the session id: find the session by endpoint then.
            session = self._engine.resolve_session(endpoint, message.session_id)
            session_id = message.session_id if session is None else session.session_id
            self._clear_reliable_pending(endpoint, session_id, message.acknowledge_number)

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
            type_flags=message.type_flags,
            command=message.command,
            datagram=datagram,
            last_sent_at=send_time,
            first_sent_at=send_time,
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
        now = time.monotonic()
        for key in keys_to_remove:
            pending = self._reliable_pending.pop(key, None)
            if pending is not None:
                self._sample_rtt(pending, now)

    def _retransmit_due(self, udp_socket: socket.socket) -> None:
        """Retransmit due reliable packets."""

        if not self._reliable_pending:
            return

        now = time.monotonic()

        for key in sorted(self._reliable_pending):
            pending = self._reliable_pending.get(key)
            if pending is None:
                continue
            if self._drop_missing_session_pending(pending):
                continue
            if self._peer_is_inactive(pending, now):
                self._timeout_session(udp_socket, pending)
                continue
            if (now - pending.last_sent_at) < self._retransmit_wait(pending):
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
            if not self._send_socket_datagram(
                udp_socket,
                datagram=pending.datagram,
                endpoint=pending.endpoint,
                action='retransmit reliable packet',
            ):
                pending.last_sent_at = now
                continue
            pending.retransmit_attempts += 1
            pending.last_sent_at = now

    def _drop_missing_session_pending(self, pending: _ReliablePending) -> bool:
        """Drop reliable transport state for sessions that no longer exist or left that endpoint.

        A packet still pending at an endpoint its session no longer uses (a
        failed login's answer after a new login from another port) is
        obsolete; timing it out would end the session's new login.
        """

        session = self._engine.resolve_session(pending.endpoint, pending.session_id)
        if session is None or session.endpoint != pending.endpoint:
            self._clear_session_pending(pending.endpoint, pending.session_id)
            self._clear_session_deferred(pending.endpoint, pending.session_id)
            self._clear_session_runtime(pending.endpoint)
            return True

        return False

    def _peer_is_inactive(self, pending: _ReliablePending, now: float) -> bool:
        """Whether the peer stayed silent for the whole timeout while it owed an ACK for this packet.

        A client sends nothing while it has nothing to say: an MH player in a
        Town only echoes every ~60 s (`0x0061d440`), an AM client echoes only
        during a race (`SystemMove` `0x001fa190`), and peers' movement needs
        no ACK. So silence counts from when this packet was first sent, which
        a live client ACKs at once.
        """

        endpoint = pending.endpoint
        last_inbound_at = self._last_inbound_at_by_endpoint.get((endpoint.host, endpoint.port))
        if last_inbound_at is None:
            return False
        silent_since = max(last_inbound_at, pending.first_sent_at)
        return (now - silent_since) >= self._SESSION_INACTIVITY_TIMEOUT_SECONDS

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

    def _forget_session_reliables(self, session_id: int) -> None:
        """Drop what one session has pending or deferred, on any port (a KICS may come from a new one)."""

        for key in [key for key in self._reliable_pending if key[2] == session_id]:
            self._reliable_pending.pop(key, None)
        for key in [key for key in self._deferred_reliable if key[2] == session_id]:
            self._deferred_reliable.pop(key, None)

    def _move_session_transport(self, old: Endpoint, new: Endpoint, session_id: int) -> None:
        """Re-address what one session still has pending or deferred to its client's new port.

        Packets sent to the old port never arrived and the client takes
        reliable packets only in order, so the pending ones are resent now.
        """

        for key in [key for key in self._reliable_pending if key[:3] == (old.host, old.port, session_id)]:
            pending = self._reliable_pending.pop(key)
            pending.endpoint = new
            # Due at once: the receive loop's resend pass right after this sends it.
            pending.last_sent_at = float('-inf')
            self._reliable_pending[(new.host, new.port, session_id, key[3])] = pending
        deferred = self._deferred_reliable.pop((old.host, old.port, session_id), None)
        if deferred:
            for message in deferred:
                message.endpoint = new
            self._deferred_reliable[(new.host, new.port, session_id)] = deferred
        rtt_estimate = self._rtt_estimate_by_endpoint.get((old.host, old.port))
        self._clear_session_runtime(old)
        if rtt_estimate is not None:
            self._rtt_estimate_by_endpoint.setdefault((new.host, new.port), rtt_estimate)

    def _clear_session_deferred(
        self,
        endpoint: Endpoint,
        session_id: int,
    ) -> None:
        """Remove deferred reliable packets for one endpoint/session."""

        self._deferred_reliable.pop((endpoint.host, endpoint.port, session_id), None)

    def _clear_session_runtime(self, endpoint: Endpoint) -> None:
        """Drop cached transport-side state for one endpoint."""

        self._footer_bytes_by_endpoint.pop((endpoint.host, endpoint.port), None)
        self._last_inbound_at_by_endpoint.pop((endpoint.host, endpoint.port), None)
        self._rtt_estimate_by_endpoint.pop((endpoint.host, endpoint.port), None)

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
        self._clear_session_deferred(pending.endpoint, pending.session_id)
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
        self._send_socket_datagram(
            udp_socket,
            datagram=datagram,
            endpoint=endpoint,
            action='send fresh datagram',
        )

    def _send_encoded_message(
        self,
        udp_socket: socket.socket,
        message: SnapMessage,
        send_time: float,
    ) -> None:
        """Encode, track, and send one outbound SNAP message."""

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

    def _send_socket_datagram(
        self,
        udp_socket: socket.socket,
        *,
        datagram: bytes,
        endpoint: Endpoint,
        action: str,
    ) -> bool:
        """Send one UDP datagram unless the socket error is truly fatal."""

        try:
            udp_socket.sendto(datagram, (endpoint.host, endpoint.port))
        except OSError as exc:
            self._logger.exception(
                'UDP sendto failed during %s to %s:%d; continuing: %s',
                action,
                endpoint.host,
                endpoint.port,
                exc,
            )
            return False
        return True

    def _should_defer_reliable(self, message: SnapMessage) -> bool:
        """Check whether a reliable packet must wait for in-flight window room."""

        if (message.type_flags & FLAG_RELIABLE) == 0:
            return False

        session_key = (message.endpoint.host, message.endpoint.port, message.session_id)
        if self._deferred_reliable.get(session_key):
            return True
        return not self._inflight_window_has_room(session_key, message.sequence_number)

    def _enqueue_deferred_reliable(self, message: SnapMessage) -> None:
        """Queue one reliable packet until the peer frees an in-flight slot."""

        session_key = (message.endpoint.host, message.endpoint.port, message.session_id)
        self._deferred_reliable.setdefault(session_key, deque()).append(message)

    def _pending_count_for_session(self, session_key: tuple[str, int, int]) -> int:
        """Count in-flight reliable packets for one endpoint/session."""

        return sum(1 for key in self._reliable_pending if key[:3] == session_key)

    def _inflight_window_has_room(
        self,
        session_key: tuple[str, int, int],
        sequence_number: int,
    ) -> bool:
        """Check whether one reliable sequence still fits the peer receive window."""

        if self._pending_count_for_session(session_key) >= self._MAX_INFLIGHT_RELIABLE_PER_SESSION:
            return False

        oldest_pending_sequence = self._oldest_pending_sequence_for_session(session_key)
        if oldest_pending_sequence is None:
            return True
        return (sequence_number - oldest_pending_sequence) < self._MAX_INFLIGHT_RELIABLE_PER_SESSION

    def _oldest_pending_sequence_for_session(self, session_key: tuple[str, int, int]) -> int | None:
        """Return the oldest still-pending reliable sequence for one endpoint/session."""

        oldest_sequence: int | None = None
        for host, port, pending_session, sequence_number in self._reliable_pending:
            if (host, port, pending_session) != session_key:
                continue
            if oldest_sequence is None or sequence_number < oldest_sequence:
                oldest_sequence = sequence_number
        return oldest_sequence

    def _flush_deferred_reliable(self, udp_socket: socket.socket, send_time: float) -> None:
        """Send deferred reliables once one endpoint/session has window room again."""

        if not self._deferred_reliable:
            return

        empty_sessions: list[tuple[str, int, int]] = []
        for session_key, deferred in self._deferred_reliable.items():
            while deferred and self._inflight_window_has_room(session_key, deferred[0].sequence_number):
                self._send_encoded_message(udp_socket, deferred.popleft(), send_time)
            if not deferred:
                empty_sessions.append(session_key)

        for session_key in empty_sessions:
            self._deferred_reliable.pop(session_key, None)
