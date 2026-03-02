"""UDP reliability transport hardening tests."""

from unittest.mock import Mock
import unittest

from opensnap.config import ServerConfig
from opensnap.protocol import commands
from opensnap.protocol.codec import encode_messages
from opensnap.protocol.constants import CHANNEL_ROOM, FLAG_RELIABLE
from opensnap.protocol.models import Endpoint, SnapMessage
from opensnap.server import SnapUdpServer


class _FakeSocket:
    """Simple socket double capturing sendto calls."""

    def __init__(self) -> None:
        self.sent: list[tuple[bytes, tuple[str, int]]] = []

    def sendto(self, payload: bytes, target: tuple[str, int]) -> None:
        self.sent.append((payload, target))


class UdpReliabilityTests(unittest.TestCase):
    """Verify reliability safeguards under lossy/high-rate conditions."""

    def _reliable_message(self, *, endpoint: Endpoint, session_id: int, sequence_number: int) -> SnapMessage:
        return SnapMessage(
            endpoint=endpoint,
            type_flags=CHANNEL_ROOM | FLAG_RELIABLE,
            packet_number=0,
            command=commands.CMD_SEND,
            session_id=session_id,
            sequence_number=sequence_number,
            acknowledge_number=0,
            payload=b'\x80\x07' + b'\x00' * 62,
        )

    def test_reliable_payload_ack_clears_pending_packets(self) -> None:
        server = SnapUdpServer(config=ServerConfig(), engine=Mock())
        endpoint = Endpoint(host='127.0.0.1', port=50000)
        session_id = 0x12345678

        for sequence in (10, 11):
            outgoing = self._reliable_message(
                endpoint=endpoint,
                session_id=session_id,
                sequence_number=sequence,
            )
            server._note_sent_sequence(outgoing)
            server._track_reliable(outgoing, b'packet', 0.0)

        incoming = SnapMessage(
            endpoint=endpoint,
            type_flags=CHANNEL_ROOM | FLAG_RELIABLE,
            packet_number=0,
            command=commands.CMD_SEND,
            session_id=session_id,
            sequence_number=999,
            acknowledge_number=11,
            payload=b'\x80\x07' + b'\x00' * 62,
        )
        server._process_transport_acks(encode_messages([incoming]), endpoint)

        self.assertEqual(len(server._reliable_pending), 0)

    def test_implausible_ack_is_ignored(self) -> None:
        server = SnapUdpServer(config=ServerConfig(), engine=Mock())
        endpoint = Endpoint(host='127.0.0.1', port=50001)
        session_id = 0x11112222

        for sequence in (20, 21):
            outgoing = self._reliable_message(
                endpoint=endpoint,
                session_id=session_id,
                sequence_number=sequence,
            )
            server._note_sent_sequence(outgoing)
            server._track_reliable(outgoing, b'packet', 0.0)

        incoming = SnapMessage(
            endpoint=endpoint,
            type_flags=CHANNEL_ROOM | FLAG_RELIABLE,
            packet_number=0,
            command=commands.CMD_SEND,
            session_id=session_id,
            sequence_number=1000,
            acknowledge_number=0x6D030000,
            payload=b'\x80\x07' + b'\x00' * 62,
        )
        server._process_transport_acks(encode_messages([incoming]), endpoint)

        self.assertEqual(len(server._reliable_pending), 2)

    def test_byte_swapped_ack_is_accepted_when_plausible(self) -> None:
        server = SnapUdpServer(config=ServerConfig(), engine=Mock())
        endpoint = Endpoint(host='127.0.0.1', port=50004)
        session_id = 0x51525354

        for sequence in (485, 486):
            outgoing = self._reliable_message(
                endpoint=endpoint,
                session_id=session_id,
                sequence_number=sequence,
            )
            server._note_sent_sequence(outgoing)
            server._track_reliable(outgoing, b'packet', 0.0)

        # 0xE6010000 byte-swaps to 0x000001E6 (486), which is plausible.
        incoming = SnapMessage(
            endpoint=endpoint,
            type_flags=CHANNEL_ROOM | FLAG_RELIABLE,
            packet_number=0,
            command=commands.CMD_SEND,
            session_id=session_id,
            sequence_number=1001,
            acknowledge_number=0xE6010000,
            payload=b'\x80\x07' + b'\x00' * 62,
        )
        server._process_transport_acks(encode_messages([incoming]), endpoint)

        self.assertEqual(len(server._reliable_pending), 0)

    def test_reliable_pending_limit_drops_oldest_sequences(self) -> None:
        server = SnapUdpServer(config=ServerConfig(), engine=Mock())
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
                server._note_sent_sequence(outgoing)
                server._track_reliable(outgoing, b'packet', 0.0)
        finally:
            server._MAX_PENDING_RELIABLE_PER_SESSION = old_limit

        kept_sequences = sorted(
            sequence
            for host, port, pending_session, sequence in server._reliable_pending
            if host == endpoint.host and port == endpoint.port and pending_session == session_id
        )
        self.assertEqual(kept_sequences, [3, 4, 5])

    def test_retransmit_budget_caps_per_cycle(self) -> None:
        server = SnapUdpServer(config=ServerConfig(), engine=Mock())
        endpoint = Endpoint(host='127.0.0.1', port=50003)
        session_id = 0x01020304
        old_budget = server._MAX_RETRANSMITS_PER_CYCLE
        server._MAX_RETRANSMITS_PER_CYCLE = 2
        try:
            for sequence in (1, 2, 3, 4, 5):
                outgoing = self._reliable_message(
                    endpoint=endpoint,
                    session_id=session_id,
                    sequence_number=sequence,
                )
                server._note_sent_sequence(outgoing)
                server._track_reliable(outgoing, b'packet', 0.0)

            fake_socket = _FakeSocket()
            server._retransmit_due(fake_socket)  # type: ignore[arg-type]
        finally:
            server._MAX_RETRANSMITS_PER_CYCLE = old_budget

        self.assertEqual(len(fake_socket.sent), 2)
        retransmit_attempts = sum(pending.retransmit_attempts for pending in server._reliable_pending.values())
        self.assertEqual(retransmit_attempts, 2)


if __name__ == '__main__':
    unittest.main()
