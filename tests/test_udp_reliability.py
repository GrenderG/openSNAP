"""UDP reliability transport hardening tests."""

from collections import deque
from unittest.mock import Mock, patch
import unittest

from opensnap.config import ServerConfig
from opensnap.protocol import commands
from opensnap.protocol.codec import decode_datagram, encode_messages
from opensnap.protocol.constants import (
    FLAG_CHANNEL_BITS,
    FLAG_LOBBY,
    FLAG_MULTI,
    FLAG_RELIABLE,
    FLAG_RESPONSE,
    FLAG_ROOM,
    FOOTER_BYTES_KAGE,
)
from opensnap.protocol.models import Endpoint, SnapMessage
from opensnap.udp_server import SnapUdpServer


class _FakeSocket:
    """Simple socket double capturing sendto calls."""

    def __init__(self) -> None:
        self.sent: list[tuple[bytes, tuple[str, int]]] = []

    def sendto(self, payload: bytes, target: tuple[str, int]) -> None:
        self.sent.append((payload, target))


class UdpReliabilityTests(unittest.TestCase):
    """Verify reliability safeguards under lossy/high-rate conditions."""

    def _server(self) -> SnapUdpServer:
        engine = Mock()
        engine.decode_datagram.side_effect = lambda payload, endpoint: __import__(
            'opensnap.protocol.codec',
            fromlist=['decode_datagram'],
        ).decode_datagram(payload, endpoint)
        engine.encode_messages.side_effect = lambda messages, footer_bytes=None: encode_messages(
            messages,
            footer_bytes=footer_bytes,
        )
        engine.handle_transport_timeout.return_value = []
        engine.reliable_timeout_seconds = 60.0
        engine.framed_sender = False
        # The session lives at the endpoint it is looked up from.
        engine.resolve_session.side_effect = lambda endpoint, session_id: Mock(
            room_id=1, endpoint=endpoint, session_id=session_id,
        )
        return SnapUdpServer(config=ServerConfig(), engine=engine)

    def _reliable_message(self, *, endpoint: Endpoint, session_id: int, sequence_number: int) -> SnapMessage:
        return SnapMessage(
            endpoint=endpoint,
            type_flags=FLAG_ROOM | FLAG_RELIABLE,
            packet_number=0,
            command=commands.CMD_SEND,
            session_id=session_id,
            sequence_number=sequence_number,
            acknowledge_number=0,
            payload=b'\x80\x07' + b'\x00' * 62,
        )

    def _control_reliable_message(
        self,
        *,
        endpoint: Endpoint,
        session_id: int,
        sequence_number: int,
    ) -> SnapMessage:
        return SnapMessage(
            endpoint=endpoint,
            type_flags=FLAG_LOBBY | FLAG_RELIABLE,
            packet_number=0,
            command=commands.CMD_SEND,
            session_id=session_id,
            sequence_number=sequence_number,
            acknowledge_number=0,
            payload=b'\x80\x07' + b'\x00' * 62,
        )

    def _mark_inbound_active(self, server: SnapUdpServer, endpoint: Endpoint, at: float) -> None:
        server._last_inbound_at_by_endpoint[(endpoint.host, endpoint.port)] = at

    def test_response_payload_ack_clears_matching_pending_packet_only(self) -> None:
        server = self._server()
        endpoint = Endpoint(host='127.0.0.1', port=50000)
        session_id = 0x12345678

        for sequence in (10, 11):
            outgoing = self._control_reliable_message(
                endpoint=endpoint,
                session_id=session_id,
                sequence_number=sequence,
            )
            server._track_reliable(outgoing, b'packet', 0.0)

        incoming = SnapMessage(
            endpoint=endpoint,
            type_flags=FLAG_ROOM | FLAG_RELIABLE | FLAG_RESPONSE,
            packet_number=0,
            command=commands.CMD_SEND,
            session_id=session_id,
            sequence_number=999,
            acknowledge_number=11,
            payload=b'\x80\x07' + b'\x00' * 62,
        )
        server._process_transport_acks(encode_messages([incoming]), endpoint)

        remaining_sequences = sorted(
            sequence
            for host, port, pending_session, sequence in server._reliable_pending
            if host == endpoint.host and port == endpoint.port and pending_session == session_id
        )
        self.assertEqual(remaining_sequences, [10])

    def test_response_ack_zero_clears_sequence_zero_pending(self) -> None:
        server = self._server()
        endpoint = Endpoint(host='127.0.0.1', port=50013)
        session_id = 0x0BADB002

        for sequence in (0, 1):
            outgoing = self._reliable_message(
                endpoint=endpoint,
                session_id=session_id,
                sequence_number=sequence,
            )
            server._track_reliable(outgoing, b'packet', 0.0)

        incoming = SnapMessage(
            endpoint=endpoint,
            type_flags=FLAG_ROOM | FLAG_RESPONSE,
            packet_number=0,
            command=commands.CMD_ACK,
            session_id=session_id,
            sequence_number=0,
            acknowledge_number=0,
            payload=b'',
        )
        server._process_transport_acks(encode_messages([incoming]), endpoint)

        remaining_sequences = sorted(
            sequence
            for host, port, pending_session, sequence in server._reliable_pending
            if host == endpoint.host and port == endpoint.port and pending_session == session_id
        )
        self.assertEqual(remaining_sequences, [1])

    def test_non_response_reliable_payload_ack_is_ignored_for_pending_retirement(self) -> None:
        server = self._server()
        endpoint = Endpoint(host='127.0.0.1', port=50008)
        session_id = 0x1234ABCD

        for sequence in (30, 31):
            outgoing = self._reliable_message(
                endpoint=endpoint,
                session_id=session_id,
                sequence_number=sequence,
            )
            server._track_reliable(outgoing, b'packet', 0.0)

        incoming = SnapMessage(
            endpoint=endpoint,
            type_flags=FLAG_ROOM | FLAG_RELIABLE,
            packet_number=0,
            command=commands.CMD_SEND,
            session_id=session_id,
            sequence_number=2000,
            acknowledge_number=31,
            payload=b'\x80\x07' + b'\x00' * 62,
        )
        server._process_transport_acks(encode_messages([incoming]), endpoint)

        remaining_sequences = sorted(
            sequence
            for host, port, pending_session, sequence in server._reliable_pending
            if host == endpoint.host and port == endpoint.port and pending_session == session_id
        )
        self.assertEqual(remaining_sequences, [30, 31])

    def test_embedded_multi_followup_ack_is_ignored_for_pending_retirement(self) -> None:
        server = self._server()
        endpoint = Endpoint(host='127.0.0.1', port=50024)
        session_id = 0x2468ACE0

        outgoing = self._reliable_message(
            endpoint=endpoint,
            session_id=session_id,
            sequence_number=31,
        )
        server._track_reliable(outgoing, b'packet', 0.0)

        outer = SnapMessage(
            endpoint=endpoint,
            type_flags=FLAG_ROOM | FLAG_RELIABLE | FLAG_MULTI,
            packet_number=0,
            command=commands.CMD_SEND,
            session_id=session_id,
            sequence_number=2000,
            acknowledge_number=0,
            payload=b'\x80\x02',
            size_word_override=(FLAG_ROOM | FLAG_RELIABLE | FLAG_MULTI) | 0x0012,
        )
        embedded = SnapMessage(
            endpoint=endpoint,
            type_flags=FLAG_ROOM | FLAG_RELIABLE,
            packet_number=0,
            command=commands.CMD_SEND,
            session_id=session_id,
            sequence_number=0,
            acknowledge_number=31,
            payload=b'',
        )

        server._process_transport_acks(encode_messages([outer, embedded]), endpoint)

        self.assertIn((endpoint.host, endpoint.port, session_id, 31), server._reliable_pending)

    def test_non_reliable_non_response_payload_ack_is_ignored_for_pending_retirement(self) -> None:
        server = self._server()
        endpoint = Endpoint(host='127.0.0.1', port=50022)
        session_id = 0x2222ABCD

        for sequence in (50, 51):
            outgoing = self._reliable_message(
                endpoint=endpoint,
                session_id=session_id,
                sequence_number=sequence,
            )
            server._track_reliable(outgoing, b'packet', 0.0)

        incoming = SnapMessage(
            endpoint=endpoint,
            type_flags=FLAG_ROOM,
            packet_number=0,
            command=commands.CMD_SEND,
            session_id=session_id,
            sequence_number=3000,
            acknowledge_number=51,
            payload=b'\x80\x07' + b'\x00' * 62,
        )
        server._process_transport_acks(encode_messages([incoming]), endpoint)

        remaining_sequences = sorted(
            sequence
            for host, port, pending_session, sequence in server._reliable_pending
            if host == endpoint.host and port == endpoint.port and pending_session == session_id
        )
        self.assertEqual(remaining_sequences, [50, 51])

    def test_bare_ack_clears_matching_pending_packet(self) -> None:
        server = self._server()
        endpoint = Endpoint(host='127.0.0.1', port=50009)
        session_id = 0x0A0B0C0D

        for sequence in (40, 41):
            outgoing = self._control_reliable_message(
                endpoint=endpoint,
                session_id=session_id,
                sequence_number=sequence,
            )
            server._track_reliable(outgoing, b'packet', 0.0)

        incoming = SnapMessage(
            endpoint=endpoint,
            type_flags=FLAG_ROOM | FLAG_RESPONSE,
            packet_number=0,
            command=commands.CMD_ACK,
            session_id=session_id,
            sequence_number=0,
            acknowledge_number=41,
            payload=b'',
        )
        server._process_transport_acks(encode_messages([incoming]), endpoint)

        remaining_sequences = sorted(
            sequence
            for host, port, pending_session, sequence in server._reliable_pending
            if host == endpoint.host and port == endpoint.port and pending_session == session_id
        )
        self.assertEqual(remaining_sequences, [40])

    def test_unknown_response_ack_is_ignored(self) -> None:
        server = self._server()
        endpoint = Endpoint(host='127.0.0.1', port=50001)
        session_id = 0x11112222

        for sequence in (20, 21):
            outgoing = self._control_reliable_message(
                endpoint=endpoint,
                session_id=session_id,
                sequence_number=sequence,
            )
            server._track_reliable(outgoing, b'packet', 0.0)

        incoming = SnapMessage(
            endpoint=endpoint,
            type_flags=FLAG_ROOM | FLAG_RELIABLE | FLAG_RESPONSE,
            packet_number=0,
            command=commands.CMD_SEND,
            session_id=session_id,
            sequence_number=1000,
            acknowledge_number=0x6D030000,
            payload=b'\x80\x07' + b'\x00' * 62,
        )
        server._process_transport_acks(encode_messages([incoming]), endpoint)

        self.assertEqual(len(server._reliable_pending), 2)

    def test_byte_swapped_ack_is_not_interpreted(self) -> None:
        server = self._server()
        endpoint = Endpoint(host='127.0.0.1', port=50004)
        session_id = 0x51525354

        for sequence in (485, 486):
            outgoing = self._control_reliable_message(
                endpoint=endpoint,
                session_id=session_id,
                sequence_number=sequence,
            )
            server._track_reliable(outgoing, b'packet', 0.0)

        incoming = SnapMessage(
            endpoint=endpoint,
            type_flags=FLAG_ROOM | FLAG_RELIABLE | FLAG_RESPONSE,
            packet_number=0,
            command=commands.CMD_SEND,
            session_id=session_id,
            sequence_number=1001,
            acknowledge_number=0xE6010000,
            payload=b'\x80\x07' + b'\x00' * 62,
        )
        server._process_transport_acks(encode_messages([incoming]), endpoint)

        remaining_sequences = sorted(
            sequence
            for host, port, pending_session, sequence in server._reliable_pending
            if host == endpoint.host and port == endpoint.port and pending_session == session_id
        )
        self.assertEqual(remaining_sequences, [485, 486])

    def test_ack_with_mismatched_session_id_does_not_clear_pending(self) -> None:
        server = self._server()
        endpoint = Endpoint(host='127.0.0.1', port=50012)
        first_session_id = 0x11112222
        mismatch_session_id = 0x55556666

        first_outgoing = self._reliable_message(
            endpoint=endpoint,
            session_id=first_session_id,
            sequence_number=77,
        )
        server._track_reliable(first_outgoing, b'packet-first', 0.0)

        incoming = SnapMessage(
            endpoint=endpoint,
            type_flags=FLAG_ROOM | FLAG_RELIABLE | FLAG_RESPONSE,
            packet_number=0,
            command=commands.CMD_SEND,
            session_id=mismatch_session_id,
            sequence_number=2001,
            acknowledge_number=77,
            payload=b'\x80\x07' + b'\x00' * 62,
        )
        server._process_transport_acks(encode_messages([incoming]), endpoint)

        self.assertIn((endpoint.host, endpoint.port, first_session_id, 77), server._reliable_pending)

    def test_higher_room_ack_clears_matching_pending_packet_only(self) -> None:
        server = self._server()
        endpoint = Endpoint(host='127.0.0.1', port=50019)
        session_id = 0x19191919

        for sequence in (10, 11, 12):
            outgoing = self._reliable_message(
                endpoint=endpoint,
                session_id=session_id,
                sequence_number=sequence,
            )
            server._track_reliable(outgoing, b'packet', 0.0)

        incoming = SnapMessage(
            endpoint=endpoint,
            type_flags=FLAG_ROOM | FLAG_RESPONSE,
            packet_number=0,
            command=commands.CMD_ACK,
            session_id=session_id,
            sequence_number=0,
            acknowledge_number=11,
            payload=b'',
        )
        server._process_transport_acks(encode_messages([incoming]), endpoint)

        remaining_sequences = sorted(
            sequence
            for host, port, pending_session, sequence in server._reliable_pending
            if host == endpoint.host and port == endpoint.port and pending_session == session_id
        )
        self.assertEqual(remaining_sequences, [10, 12])

    def test_resend_wait_follows_the_mh_sender(self) -> None:
        # MH-family sender: last send + RTT estimate + (len + 131) // 100 ms +
        # backoff 17 ms growing by 200 ms per resend (`0x001fd2e8..0x001fd328`).
        server = self._server()
        endpoint = Endpoint(host='127.0.0.1', port=50010)
        session_id = 0x10101010
        server._track_reliable(
            self._reliable_message(endpoint=endpoint, session_id=session_id, sequence_number=77), b'p' * 69, 100.0,
        )
        pending = server._reliable_pending[(endpoint.host, endpoint.port, session_id, 77)]
        self._mark_inbound_active(server, endpoint, 100.0)

        # Before any ACK the estimate is the games' INITIAL_LATENCY 1600 ms.
        self.assertAlmostEqual(server._retransmit_wait(pending), 1.6 + 0.002 + 0.017)
        server._rtt_estimate_by_endpoint[(endpoint.host, endpoint.port)] = 0.210
        pending.retransmit_attempts = 2
        self.assertAlmostEqual(server._retransmit_wait(pending), 0.210 + 0.002 + 0.017 + 0.4)

        fake_socket = _FakeSocket()
        with patch('opensnap.udp_server.time.monotonic', return_value=100.6):
            server._retransmit_due(fake_socket)  # type: ignore[arg-type]
        self.assertEqual(fake_socket.sent, [])
        with patch('opensnap.udp_server.time.monotonic', return_value=100.63):
            server._retransmit_due(fake_socket)  # type: ignore[arg-type]
        self.assertEqual(len(fake_socket.sent), 1)
        self.assertEqual(pending.retransmit_attempts, 3)

    def test_ack_replaces_the_rtt_estimate_like_the_mh_sender(self) -> None:
        # `0x00212e78`: each ACK's sample replaces the estimate; a resent packet's
        # sample adds the waits spent on it (`0x00209800..0x00209834`).
        server = self._server()
        endpoint = Endpoint(host='127.0.0.1', port=50012)
        session_id = 0x12121212
        key = (endpoint.host, endpoint.port)
        for sequence in (1, 2):
            server._track_reliable(
                self._reliable_message(endpoint=endpoint, session_id=session_id, sequence_number=sequence),
                b'packet',
                100.0,
            )

        with patch('opensnap.udp_server.time.monotonic', return_value=100.21):
            server._clear_reliable_pending(endpoint, session_id, 1)
        self.assertAlmostEqual(server._rtt_estimate_by_endpoint[key], 0.21)

        resent = server._reliable_pending[(endpoint.host, endpoint.port, session_id, 2)]
        resent.retransmit_attempts = 1
        resent.last_sent_at = 100.5
        with patch('opensnap.udp_server.time.monotonic', return_value=100.71):
            server._clear_reliable_pending(endpoint, session_id, 2)
        self.assertAlmostEqual(server._rtt_estimate_by_endpoint[key], 0.21 + 0.21 + 0.217)

    def test_packet_whose_acks_were_lost_is_resent_until_acked(self) -> None:
        # `11 10 2026 1756.pcapng`: all five ACKs of seq 948 were lost on the way
        # up; parking it after four retries froze every reliable packet from
        # 948 + 16 on while the client kept talking. Resent, the client ACKs
        # its copy again and the stream goes on.
        server = self._server()
        endpoint = Endpoint(host='127.0.0.1', port=50014)
        session_id = 0x14141414
        server._track_reliable(
            self._reliable_message(endpoint=endpoint, session_id=session_id, sequence_number=948), b'packet', 0.0,
        )
        key = (endpoint.host, endpoint.port, session_id, 948)
        pending = server._reliable_pending[key]
        pending.retransmit_attempts = 4
        pending.last_sent_at = 100.0
        self._mark_inbound_active(server, endpoint, 102.9)

        fake_socket = _FakeSocket()
        with patch('opensnap.udp_server.time.monotonic', return_value=103.0):
            server._retransmit_due(fake_socket)  # type: ignore[arg-type]

        self.assertEqual(len(fake_socket.sent), 1)
        server._engine.handle_transport_timeout.assert_not_called()
        server._clear_reliable_pending(endpoint, session_id, 948)
        self.assertNotIn(key, server._reliable_pending)

    def test_pending_packet_times_out_after_peer_inactivity_window(self) -> None:
        server = self._server()
        endpoint = Endpoint(host='127.0.0.1', port=50016)
        session_id = 0x16161616
        server._track_reliable(
            self._control_reliable_message(endpoint=endpoint, session_id=session_id, sequence_number=100),
            b'packet',
            0.0,
        )
        key = (endpoint.host, endpoint.port, session_id, 100)
        server._reliable_pending[key].last_sent_at = 104.0

        self._mark_inbound_active(server, endpoint, 100.0)
        fake_socket = _FakeSocket()
        with patch(
            'opensnap.udp_server.time.monotonic',
            return_value=100.0 + server._engine.reliable_timeout_seconds + 0.1,
        ):
            server._retransmit_due(fake_socket)  # type: ignore[arg-type]

        self.assertEqual(len(fake_socket.sent), 0)
        self.assertNotIn(key, server._reliable_pending)
        server._engine.handle_transport_timeout.assert_called_once_with(endpoint, session_id)

    def test_session_moved_to_a_new_port_takes_its_pending_packets_along(self) -> None:
        # A packet left pending at the old port no longer times the session out
        # (the old port is silent for good) and is resent to the new port now.
        server = self._server()
        old = Endpoint(host='127.0.0.1', port=50018)
        new = Endpoint(host='127.0.0.1', port=50019)
        session_id = 0x18181818
        server._track_reliable(
            self._reliable_message(endpoint=old, session_id=session_id, sequence_number=102), b'packet', 0.0,
        )
        server._deferred_reliable[(old.host, old.port, session_id)] = deque(
            [self._reliable_message(endpoint=old, session_id=session_id, sequence_number=118)]
        )
        self._mark_inbound_active(server, old, 0.0)
        self._mark_inbound_active(server, new, 200.0)

        with patch('opensnap.udp_server.time.monotonic', return_value=200.0):
            server._move_session_transport(old, new, session_id)
            fake_socket = _FakeSocket()
            server._retransmit_due(fake_socket)  # type: ignore[arg-type]

        self.assertEqual(fake_socket.sent, [(b'packet', (new.host, new.port))])
        self.assertIn((new.host, new.port, session_id, 102), server._reliable_pending)
        self.assertEqual([message.endpoint for message in server._deferred_reliable[(new.host, new.port, session_id)]],
                         [new])
        server._engine.handle_transport_timeout.assert_not_called()

    def test_quiet_client_is_not_timed_out_by_a_packet_it_was_just_sent(self) -> None:
        # snap00.pcap: a hunter idle in a Town sent nothing for 60.1 s (its
        # peers' movement needs no ACK); a reliable relay queued for it then was
        # timed out at once, before it could ACK. Silence counts from the send.
        server = self._server()
        endpoint = Endpoint(host='2.245.17.21', port=55639)
        session_id = 0x8F3DA84A
        self._mark_inbound_active(server, endpoint, 0.0)
        server._track_reliable(
            self._reliable_message(endpoint=endpoint, session_id=session_id, sequence_number=8882), b'packet', 60.1,
        )

        with patch('opensnap.udp_server.time.monotonic', return_value=60.1):
            server._retransmit_due(_FakeSocket())  # type: ignore[arg-type]
        server._engine.handle_transport_timeout.assert_not_called()
        self.assertIn((endpoint.host, endpoint.port, session_id, 8882), server._reliable_pending)

        # A peer that never answers it is still timed out, a full window after the send.
        with patch('opensnap.udp_server.time.monotonic', return_value=60.2 + server._engine.reliable_timeout_seconds):
            server._retransmit_due(_FakeSocket())  # type: ignore[arg-type]
        server._engine.handle_transport_timeout.assert_called_once_with(endpoint, session_id)

    def test_unacknowledged_keepalive_is_resent_and_never_times_the_session_out(self) -> None:
        # AM Beta1 stops polling between a race's finish and the lobby; it ACKs
        # the keepalive later. An ordinary packet still times the session out.
        server = self._server()
        endpoint = Endpoint(host='170.253.44.213', port=2000)
        session_id = 0x61384AE8
        self._mark_inbound_active(server, endpoint, 0.0)
        keepalive = SnapMessage(
            endpoint=endpoint,
            type_flags=FLAG_RELIABLE,
            packet_number=0,
            command=commands.CMD_ACK,
            session_id=session_id,
            sequence_number=7,
            acknowledge_number=0,
        )
        server._track_reliable(keepalive, b'keepalive', 20.0)

        late = 20.0 + server._engine.reliable_timeout_seconds * 3
        with patch('opensnap.udp_server.time.monotonic', return_value=late):
            fake_socket = _FakeSocket()
            server._retransmit_due(fake_socket)  # type: ignore[arg-type]
        self.assertEqual(fake_socket.sent, [(b'keepalive', (endpoint.host, endpoint.port))])
        self.assertIn((endpoint.host, endpoint.port, session_id, 7), server._reliable_pending)
        server._engine.handle_transport_timeout.assert_not_called()

        server._track_reliable(
            self._reliable_message(endpoint=endpoint, session_id=session_id, sequence_number=8), b'packet', late,
        )
        with patch(
            'opensnap.udp_server.time.monotonic',
            return_value=late + server._engine.reliable_timeout_seconds + 0.1,
        ):
            server._retransmit_due(_FakeSocket())  # type: ignore[arg-type]
        server._engine.handle_transport_timeout.assert_called_once_with(endpoint, session_id)

    def test_a_longer_plugin_timeout_lets_a_silent_client_ack_late(self) -> None:
        # AM Beta1 does not poll on the results and car save screens: a packet
        # pending for 200 s is still ACKed in time under its 300 s timeout.
        server = self._server()
        server._engine.reliable_timeout_seconds = 300.0
        endpoint = Endpoint(host='170.253.44.213', port=2000)
        session_id = 0x61384AE8
        self._mark_inbound_active(server, endpoint, 0.0)
        server._track_reliable(
            self._reliable_message(endpoint=endpoint, session_id=session_id, sequence_number=9), b'packet', 0.0,
        )

        with patch('opensnap.udp_server.time.monotonic', return_value=200.0):
            server._retransmit_due(_FakeSocket())  # type: ignore[arg-type]
        server._engine.handle_transport_timeout.assert_not_called()
        server._clear_reliable_pending(endpoint, session_id, 9)
        self.assertEqual(server._reliable_pending, {})

        server._track_reliable(
            self._reliable_message(endpoint=endpoint, session_id=session_id, sequence_number=10), b'packet', 200.0,
        )
        with patch('opensnap.udp_server.time.monotonic', return_value=499.0):
            server._retransmit_due(_FakeSocket())  # type: ignore[arg-type]
        server._engine.handle_transport_timeout.assert_not_called()
        with patch('opensnap.udp_server.time.monotonic', return_value=500.1):
            server._retransmit_due(_FakeSocket())  # type: ignore[arg-type]
        server._engine.handle_transport_timeout.assert_called_once_with(endpoint, session_id)

    def _framed_server(self, endpoint: Endpoint, session_id: int) -> SnapUdpServer:
        # AM Beta1: the client reported line speed 7 at its KICS login.
        server = self._server()
        server._engine.framed_sender = True
        server._engine.resolve_session.side_effect = lambda found_at, header_id: Mock(
            session_id=session_id, endpoint=endpoint, line_speed=7,
        )
        return server

    def _pump_at(self, server: SnapUdpServer, at: float) -> list[bytes]:
        fake_socket = _FakeSocket()
        server._next_frame_at = 0.0
        with patch('opensnap.udp_server.time.monotonic', return_value=at):
            server._pump_framed_links(fake_socket)  # type: ignore[arg-type]
        return [payload for payload, _ in fake_socket.sent]

    def test_framed_client_gets_one_datagram_per_frame_after_the_sdk_pacing(self) -> None:
        # Beta1 merges datagrams waiting at one poll and drops what follows a
        # leading bare ACK; the SDK's own sender never sends two in one frame.
        endpoint = Endpoint(host='170.253.44.213', port=1026)
        session_id = 0x61384AE8
        server = self._framed_server(endpoint, session_id)
        relays = [
            self._reliable_message(endpoint=endpoint, session_id=session_id, sequence_number=sequence)
            for sequence in (134, 135)
        ]
        server._send_messages(_FakeSocket(), relays)  # type: ignore[arg-type]

        first = self._pump_at(server, 100.0)
        self.assertEqual([decode_datagram(d, endpoint)[0].sequence_number for d in first], [134])
        # 17 ms of pacing, max(17, (80 + 7 + 31) // 7), but never the next frame.
        self.assertEqual(self._pump_at(server, 100.0167), [])
        second = self._pump_at(server, 100.0334)
        self.assertEqual([decode_datagram(d, endpoint)[0].sequence_number for d in second], [135])

    def test_framed_client_gets_no_ack_in_the_frame_after_a_datagram(self) -> None:
        # A bare ACK sent one frame after a relay can reach the client within
        # one poll; Beta1 reads only the relay's header, the ACK is lost and the
        # game fails with error 601 16 packets later.
        endpoint = Endpoint(host='170.253.44.213', port=2000)
        session_id = 0x61384AE8
        server = self._framed_server(endpoint, session_id)
        relay = self._reliable_message(endpoint=endpoint, session_id=session_id, sequence_number=5351)
        server._send_messages(_FakeSocket(), [relay])  # type: ignore[arg-type]
        self.assertEqual(len(self._pump_at(server, 100.0)), 1)
        ack = SnapMessage(
            endpoint=endpoint,
            type_flags=FLAG_ROOM | FLAG_RESPONSE,
            packet_number=0,
            command=commands.CMD_ACK,
            session_id=session_id,
            sequence_number=0,
            acknowledge_number=5341,
        )
        server._send_messages(_FakeSocket(), [ack])  # type: ignore[arg-type]

        self.assertEqual(self._pump_at(server, 100.0167), [])
        (datagram,) = self._pump_at(server, 100.0334)
        self.assertEqual(decode_datagram(datagram, endpoint)[0].acknowledge_number, 5341)

    def test_framed_client_gets_its_ack_on_the_next_queued_datagram(self) -> None:
        endpoint = Endpoint(host='170.253.44.213', port=1026)
        session_id = 0x61384AE8
        server = self._framed_server(endpoint, session_id)
        relay = self._reliable_message(endpoint=endpoint, session_id=session_id, sequence_number=134)
        ack = SnapMessage(
            endpoint=endpoint,
            type_flags=FLAG_ROOM | FLAG_RESPONSE,
            packet_number=0,
            command=commands.CMD_ACK,
            session_id=session_id,
            sequence_number=0,
            acknowledge_number=137,
        )
        server._send_messages(_FakeSocket(), [relay, ack])  # type: ignore[arg-type]

        (datagram,) = self._pump_at(server, 100.0)
        (sent,) = decode_datagram(datagram, endpoint)
        self.assertEqual((sent.command, sent.sequence_number, sent.acknowledge_number), (commands.CMD_SEND, 134, 137))
        self.assertTrue(sent.type_flags & FLAG_RESPONSE)
        self.assertEqual(self._pump_at(server, 101.0), [])

    def test_framed_resend_carries_only_an_ack_owed_now(self) -> None:
        # The first copy's ACK is stale once that copy arrived; Beta1's
        # `kkSetRevAck` would stop resending the client's packets (0x002ebaf4).
        endpoint = Endpoint(host='170.253.44.213', port=1026)
        session_id = 0x61384AE8
        server = self._framed_server(endpoint, session_id)
        self._mark_inbound_active(server, endpoint, 100.0)
        relay = self._reliable_message(endpoint=endpoint, session_id=session_id, sequence_number=134)
        server._send_messages(_FakeSocket(), [relay, self._framed_ack(endpoint, session_id, 137)])  # type: ignore[arg-type]
        (first,) = self._pump_at(server, 100.0)
        self.assertEqual(decode_datagram(first, endpoint)[0].acknowledge_number, 137)

        with patch('opensnap.udp_server.time.monotonic', return_value=110.0):
            server._retransmit_due(_FakeSocket())  # type: ignore[arg-type]
        (resend,) = self._pump_at(server, 110.0) + self._pump_at(server, 110.0)
        self.assertFalse(decode_datagram(resend, endpoint)[0].type_flags & FLAG_RESPONSE)

        with patch('opensnap.udp_server.time.monotonic', return_value=120.0):
            server._retransmit_due(_FakeSocket())  # type: ignore[arg-type]
        server._send_messages(_FakeSocket(), [self._framed_ack(endpoint, session_id, 138)])  # type: ignore[arg-type]
        (owed,) = self._pump_at(server, 120.0) + self._pump_at(server, 120.0)
        (sent,) = decode_datagram(owed, endpoint)
        self.assertEqual((sent.sequence_number, sent.acknowledge_number), (134, 138))
        self.assertTrue(sent.type_flags & FLAG_RESPONSE)

    @staticmethod
    def _framed_ack(endpoint: Endpoint, session_id: int, acknowledge_number: int) -> SnapMessage:
        return SnapMessage(
            endpoint=endpoint,
            type_flags=FLAG_ROOM | FLAG_RESPONSE,
            packet_number=0,
            command=commands.CMD_ACK,
            session_id=session_id,
            sequence_number=0,
            acknowledge_number=acknowledge_number,
        )

    def test_ack_with_the_clients_pre_login_word_clears_the_answer(self) -> None:
        # snap00.pcap: the bootstrap's login failure (0x2e) was ACKed at once with
        # header session 0x6fe607d0, the client's own word before KICS, and was
        # resent for 60 s regardless.
        server = self._server()
        endpoint = Endpoint(host='2.245.17.21', port=61342)
        session_id = 0x8F3DA84A
        server._engine.resolve_session.side_effect = lambda found_at, header_id: (
            Mock(endpoint=found_at, session_id=session_id) if found_at == endpoint else None
        )
        server._track_reliable(
            self._control_reliable_message(endpoint=endpoint, session_id=session_id, sequence_number=0), b'fail', 0.0,
        )

        ack = SnapMessage(endpoint=endpoint, type_flags=FLAG_ROOM | FLAG_RESPONSE, packet_number=0,
                          command=commands.CMD_ACK, session_id=0x6FE607D0, sequence_number=0,
                          acknowledge_number=0, payload=b'')
        server._process_transport_acks(encode_messages([ack]), endpoint)

        self.assertEqual(server._reliable_pending, {})

    def test_pending_at_a_port_its_session_left_never_ends_the_session(self) -> None:
        # snap00.pcap: that unmatched 0x2e at port 61342 timed out after the
        # player had logged in again from 51687 and ended the new login.
        server = self._server()
        old = Endpoint(host='2.245.17.21', port=61342)
        new = Endpoint(host='2.245.17.21', port=51687)
        session_id = 0x8F3DA84A
        server._engine.resolve_session.side_effect = lambda endpoint, header_id: Mock(
            endpoint=new, session_id=session_id,
        )
        server._track_reliable(
            self._control_reliable_message(endpoint=old, session_id=session_id, sequence_number=0), b'fail', 0.0,
        )
        self._mark_inbound_active(server, old, 0.0)

        with patch('opensnap.udp_server.time.monotonic', return_value=61.0):
            server._retransmit_due(_FakeSocket())  # type: ignore[arg-type]

        self.assertEqual(server._reliable_pending, {})
        server._engine.handle_transport_timeout.assert_not_called()

    def test_pending_for_missing_session_is_cleared(self) -> None:
        server = self._server()
        endpoint = Endpoint(host='127.0.0.1', port=50017)
        session_id = 0x17171717
        server._track_reliable(
            self._reliable_message(endpoint=endpoint, session_id=session_id, sequence_number=101), b'packet', 0.0,
        )
        key = (endpoint.host, endpoint.port, session_id, 101)
        server._engine.resolve_session.side_effect = lambda endpoint, session_id: None

        fake_socket = _FakeSocket()
        with patch('opensnap.udp_server.time.monotonic', return_value=105.1):
            server._retransmit_due(fake_socket)  # type: ignore[arg-type]

        self.assertEqual(len(fake_socket.sent), 0)
        self.assertNotIn(key, server._reliable_pending)
        server._engine.handle_transport_timeout.assert_not_called()

    def test_reliable_send_defers_once_inflight_window_is_full(self) -> None:
        server = self._server()
        endpoint = Endpoint(host='127.0.0.1', port=50020)
        session_id = 0x20202021
        fake_socket = _FakeSocket()

        messages = [
            self._reliable_message(
                endpoint=endpoint,
                session_id=session_id,
                sequence_number=sequence,
            )
            for sequence in range(server._MAX_INFLIGHT_RELIABLE_PER_SESSION + 1)
        ]

        with patch('opensnap.udp_server.time.monotonic', return_value=20.0):
            server._send_messages(fake_socket, messages)  # type: ignore[arg-type]

        self.assertEqual(len(fake_socket.sent), server._MAX_INFLIGHT_RELIABLE_PER_SESSION)
        self.assertIn(
            (endpoint.host, endpoint.port, session_id, server._MAX_INFLIGHT_RELIABLE_PER_SESSION - 1),
            server._reliable_pending,
        )
        self.assertNotIn(
            (endpoint.host, endpoint.port, session_id, server._MAX_INFLIGHT_RELIABLE_PER_SESSION),
            server._reliable_pending,
        )
        self.assertEqual(
            len(server._deferred_reliable[(endpoint.host, endpoint.port, session_id)]),
            1,
        )

    def test_exact_ack_flushes_one_deferred_reliable_when_window_slot_opens(self) -> None:
        server = self._server()
        endpoint = Endpoint(host='127.0.0.1', port=50021)
        session_id = 0x21212121
        fake_socket = _FakeSocket()

        messages = [
            self._reliable_message(
                endpoint=endpoint,
                session_id=session_id,
                sequence_number=sequence,
            )
            for sequence in range(server._MAX_INFLIGHT_RELIABLE_PER_SESSION + 1)
        ]
        with patch('opensnap.udp_server.time.monotonic', return_value=30.0):
            server._send_messages(fake_socket, messages)  # type: ignore[arg-type]

        ack = SnapMessage(
            endpoint=endpoint,
            type_flags=FLAG_ROOM | FLAG_RESPONSE,
            packet_number=0,
            command=commands.CMD_ACK,
            session_id=session_id,
            sequence_number=0,
            acknowledge_number=0,
            payload=b'',
        )
        server._process_transport_acks(encode_messages([ack]), endpoint)

        with patch('opensnap.udp_server.time.monotonic', return_value=30.1):
            server._send_messages(fake_socket, [])  # type: ignore[arg-type]

        self.assertIn(
            (endpoint.host, endpoint.port, session_id, server._MAX_INFLIGHT_RELIABLE_PER_SESSION),
            server._reliable_pending,
        )
        self.assertNotIn(
            (endpoint.host, endpoint.port, session_id, 0),
            server._reliable_pending,
        )
        self.assertNotIn((endpoint.host, endpoint.port, session_id), server._deferred_reliable)

    def test_reliable_send_defers_when_oldest_gap_has_15_later_sequences(self) -> None:
        server = self._server()
        endpoint = Endpoint(host='127.0.0.1', port=50024)
        session_id = 0x24242424
        fake_socket = _FakeSocket()

        initial_messages = [
            self._reliable_message(
                endpoint=endpoint,
                session_id=session_id,
                sequence_number=sequence,
            )
            for sequence in range(100, 116)
        ]
        with patch('opensnap.udp_server.time.monotonic', return_value=40.0):
            server._send_messages(fake_socket, initial_messages)  # type: ignore[arg-type]

        for sequence in range(101, 116):
            server._clear_reliable_pending(endpoint, session_id, sequence)

        blocked = self._reliable_message(
            endpoint=endpoint,
            session_id=session_id,
            sequence_number=116,
        )
        with patch('opensnap.udp_server.time.monotonic', return_value=40.1):
            server._send_messages(fake_socket, [blocked])  # type: ignore[arg-type]

        self.assertNotIn((endpoint.host, endpoint.port, session_id, 116), server._reliable_pending)
        self.assertEqual(
            [queued.sequence_number for queued in server._deferred_reliable[(endpoint.host, endpoint.port, session_id)]],
            [116],
        )

        server._clear_reliable_pending(endpoint, session_id, 100)
        with patch('opensnap.udp_server.time.monotonic', return_value=40.2):
            server._send_messages(fake_socket, [])  # type: ignore[arg-type]

        self.assertIn((endpoint.host, endpoint.port, session_id, 116), server._reliable_pending)
        self.assertNotIn((endpoint.host, endpoint.port, session_id), server._deferred_reliable)

    def test_retransmit_resends_every_due_pending_packet(self) -> None:
        # The SDK sender walks every unacknowledged slot (AM `kkSendOperation`
        # `0x002ea770..0x002ea980`), not only the oldest.
        server = self._server()
        endpoint = Endpoint(host='127.0.0.1', port=50011)
        session_id = 0x20202020
        for sequence, datagram in ((100, b'oldest'), (101, b'newer')):
            server._track_reliable(
                self._reliable_message(endpoint=endpoint, session_id=session_id, sequence_number=sequence),
                datagram,
                100.0,
            )

        fake_socket = _FakeSocket()
        with patch('opensnap.udp_server.time.monotonic', return_value=102.0):
            server._retransmit_due(fake_socket)  # type: ignore[arg-type]

        self.assertEqual([datagram for datagram, _ in fake_socket.sent], [b'oldest', b'newer'])

    def test_retransmit_due_processes_all_due_sessions(self) -> None:
        server = self._server()
        endpoint = Endpoint(host='127.0.0.1', port=50003)
        for session_id, sequence in (
            (0x01020304, 1),
            (0x01020305, 1),
            (0x01020306, 1),
        ):
            outgoing = self._reliable_message(
                endpoint=endpoint,
                session_id=session_id,
                sequence_number=sequence,
            )
            server._track_reliable(outgoing, b'packet', 0.0)

        fake_socket = _FakeSocket()
        server._retransmit_due(fake_socket)  # type: ignore[arg-type]

        self.assertEqual(len(fake_socket.sent), 3)
        retransmit_attempts = sum(pending.retransmit_attempts for pending in server._reliable_pending.values())
        self.assertEqual(retransmit_attempts, 3)

    def test_send_messages_uses_observed_kage_footer_for_endpoint(self) -> None:
        server = self._server()
        endpoint = Endpoint(host='127.0.0.1', port=50005)
        fake_socket = _FakeSocket()

        inbound = self._reliable_message(
            endpoint=endpoint,
            session_id=0x22223333,
            sequence_number=1,
        )
        server._remember_footer_variant(encode_messages([inbound], footer_bytes=FOOTER_BYTES_KAGE), endpoint)

        outbound = self._reliable_message(
            endpoint=endpoint,
            session_id=0x22223333,
            sequence_number=2,
        )
        server._send_messages(fake_socket, [outbound])  # type: ignore[arg-type]

        self.assertEqual(len(fake_socket.sent), 1)
        self.assertTrue(fake_socket.sent[0][0].endswith(FOOTER_BYTES_KAGE))

    def test_engine_errors_log_inbound_hexdump(self) -> None:
        server = self._server()
        endpoint = Endpoint(host='127.0.0.1', port=50006)
        payload = b'\x01\x02'

        with self.assertLogs('opensnap.game', level='ERROR') as captured:
            server._log_engine_errors(endpoint, payload, ['Unhandled command 0x99'])

        lines = '\n'.join(captured.output)
        self.assertIn('Inbound hexdump for engine error from 127.0.0.1:50006', lines)
        self.assertIn('01 02', lines)
        self.assertIn('Engine error from 127.0.0.1:50006: Unhandled command 0x99', lines)

    def test_send_messages_keeps_query_attribute_replies_split_by_default(self) -> None:
        server = self._server()
        endpoint = Endpoint(host='127.0.0.1', port=50007)
        fake_socket = _FakeSocket()

        first = SnapMessage(
            endpoint=endpoint,
            type_flags=FLAG_CHANNEL_BITS | FLAG_RESPONSE,
            packet_number=0,
            command=commands.CMD_QUERY_ATTRIBUTE,
            session_id=0x11112222,
            sequence_number=10,
            acknowledge_number=7,
            payload=b'\x00\x00\x00\x01USER\x00\x00\x00\x00',
        )
        second = SnapMessage(
            endpoint=endpoint,
            type_flags=FLAG_CHANNEL_BITS | FLAG_RESPONSE,
            packet_number=1,
            command=commands.CMD_QUERY_ATTRIBUTE,
            session_id=0x11112222,
            sequence_number=11,
            acknowledge_number=7,
            payload=b'\x00\x00\x00\x02USER\x00\x00\x00\x00',
        )

        server._send_messages(fake_socket, [first, second])  # type: ignore[arg-type]

        self.assertEqual(len(fake_socket.sent), 2)
        for sent_payload, sent_target in fake_socket.sent:
            self.assertEqual(sent_target, (endpoint.host, endpoint.port))
            decoded = decode_datagram(sent_payload, endpoint)
            self.assertEqual(len(decoded), 1)
            self.assertEqual(decoded[0].command, commands.CMD_QUERY_ATTRIBUTE)

    def test_idle_endpoints_without_a_session_are_forgotten(self) -> None:
        server = self._server()
        scanner = Endpoint(host='10.0.0.1', port=40000)
        player = Endpoint(host='10.0.0.2', port=40001)
        server._engine.has_session_at.side_effect = lambda endpoint: endpoint == player
        for endpoint in (scanner, player):
            self._mark_inbound_active(server, endpoint, 0.0)
            server._footer_bytes_by_endpoint[(endpoint.host, endpoint.port)] = FOOTER_BYTES_KAGE

        server._forget_idle_endpoints(59.0)
        self.assertIn(('10.0.0.1', 40000), server._last_inbound_at_by_endpoint)
        server._forget_idle_endpoints(60.0)
        self.assertNotIn(('10.0.0.1', 40000), server._last_inbound_at_by_endpoint)
        self.assertNotIn(('10.0.0.1', 40000), server._footer_bytes_by_endpoint)
        self.assertIn(('10.0.0.2', 40001), server._last_inbound_at_by_endpoint)
        self.assertIn(('10.0.0.2', 40001), server._footer_bytes_by_endpoint)


if __name__ == '__main__':
    unittest.main()
