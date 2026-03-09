"""UDP reliability transport hardening tests."""

from unittest.mock import Mock, patch
import unittest

from opensnap.config import ServerConfig
from opensnap.protocol import commands
from opensnap.protocol.codec import decode_datagram, encode_messages
from opensnap.protocol.constants import FLAG_CHANNEL_BITS, FLAG_RELIABLE, FLAG_RESPONSE, FLAG_ROOM, FOOTER_BYTES_KAGE
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

    def test_response_payload_ack_clears_matching_pending_packet_only(self) -> None:
        server = self._server()
        endpoint = Endpoint(host='127.0.0.1', port=50000)
        session_id = 0x12345678

        for sequence in (10, 11):
            outgoing = self._reliable_message(
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

    def test_bare_ack_clears_matching_pending_packet(self) -> None:
        server = self._server()
        endpoint = Endpoint(host='127.0.0.1', port=50009)
        session_id = 0x0A0B0C0D

        for sequence in (40, 41):
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
            outgoing = self._reliable_message(
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
            outgoing = self._reliable_message(
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

    def test_reliable_pending_limit_drops_oldest_sequences(self) -> None:
        server = self._server()
        endpoint = Endpoint(host='127.0.0.1', port=50002)
        session_id = 0xABCDEF01
        old_limit = server._MAX_PENDING_RELIABLE_PER_SESSION
        server._MAX_PENDING_RELIABLE_PER_SESSION = 3
        try:
            for sequence in (1, 2, 3, 4, 5):
                outgoing = self._reliable_message(
                    endpoint=endpoint,
                    session_id=session_id,
                    sequence_number=sequence,
                )
                server._track_reliable(outgoing, b'packet', 0.0)
        finally:
            server._MAX_PENDING_RELIABLE_PER_SESSION = old_limit

        kept_sequences = sorted(
            sequence
            for host, port, pending_session, sequence in server._reliable_pending
            if host == endpoint.host and port == endpoint.port and pending_session == session_id
        )
        self.assertEqual(kept_sequences, [3, 4, 5])

    def test_retry_capped_packet_uses_timeout_cadence(self) -> None:
        server = self._server()
        endpoint = Endpoint(host='127.0.0.1', port=50010)
        session_id = 0x10101010
        outgoing = self._reliable_message(
            endpoint=endpoint,
            session_id=session_id,
            sequence_number=77,
        )
        server._track_reliable(outgoing, b'packet', 0.0)

        key = (endpoint.host, endpoint.port, session_id, 77)
        pending = server._reliable_pending[key]
        pending.retransmit_attempts = server._MAX_RETRANSMIT_ATTEMPTS

        fake_socket = _FakeSocket()
        pending.last_sent_at = 100.3
        with patch('opensnap.udp_server.time.monotonic', return_value=100.5):
            server._retransmit_due(fake_socket)  # type: ignore[arg-type]

        self.assertEqual(len(fake_socket.sent), 0)
        self.assertIn(key, server._reliable_pending)

        with patch('opensnap.udp_server.time.monotonic', return_value=101.4):
            server._retransmit_due(fake_socket)  # type: ignore[arg-type]

        self.assertEqual(len(fake_socket.sent), 1)
        self.assertIn(key, server._reliable_pending)

    def test_retransmit_uses_oldest_pending_per_session_only(self) -> None:
        server = self._server()
        endpoint = Endpoint(host='127.0.0.1', port=50011)
        session_id = 0x20202020

        oldest = self._reliable_message(
            endpoint=endpoint,
            session_id=session_id,
            sequence_number=100,
        )
        newer = self._reliable_message(
            endpoint=endpoint,
            session_id=session_id,
            sequence_number=101,
        )
        server._track_reliable(oldest, b'oldest', 0.0)
        server._track_reliable(newer, b'newer', 0.0)

        oldest_key = (endpoint.host, endpoint.port, session_id, 100)
        newer_key = (endpoint.host, endpoint.port, session_id, 101)
        server._reliable_pending[oldest_key].retransmit_attempts = server._MAX_RETRANSMIT_ATTEMPTS
        server._reliable_pending[newer_key].retransmit_attempts = server._MAX_RETRANSMIT_ATTEMPTS

        fake_socket = _FakeSocket()
        with patch('opensnap.udp_server.time.monotonic', return_value=101.0):
            server._retransmit_due(fake_socket)  # type: ignore[arg-type]

        self.assertIn(oldest_key, server._reliable_pending)
        self.assertIn(newer_key, server._reliable_pending)
        self.assertEqual(len(fake_socket.sent), 1)
        self.assertEqual(fake_socket.sent[0][0], b'oldest')

    def test_retransmit_budget_caps_per_cycle(self) -> None:
        server = self._server()
        endpoint = Endpoint(host='127.0.0.1', port=50003)
        old_budget = server._MAX_RETRANSMITS_PER_CYCLE
        server._MAX_RETRANSMITS_PER_CYCLE = 2
        try:
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
        finally:
            server._MAX_RETRANSMITS_PER_CYCLE = old_budget

        self.assertEqual(len(fake_socket.sent), 2)
        retransmit_attempts = sum(pending.retransmit_attempts for pending in server._reliable_pending.values())
        self.assertEqual(retransmit_attempts, 2)

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


if __name__ == '__main__':
    unittest.main()
