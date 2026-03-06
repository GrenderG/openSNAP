"""Protocol codec tests."""

import struct
import unittest

from opensnap.protocol.codec import PacketDecodeError, decode_datagram, detect_footer_bytes, encode_messages
from opensnap.protocol.constants import (
    FLAG_CHANNEL_BITS,
    FLAG_ROOM,
    FLAG_MULTI,
    FLAG_RESPONSE,
    FOOTER_BYTES_KAGE,
    FOOTER_BYTES,
)
from opensnap.protocol.models import Endpoint, SnapMessage


class CodecTests(unittest.TestCase):
    """Datagram encoding and decoding tests."""

    def test_round_trip_single_message(self) -> None:
        endpoint = Endpoint(host='127.0.0.1', port=12345)
        message = SnapMessage(
            endpoint=endpoint,
            type_flags=FLAG_CHANNEL_BITS | FLAG_RESPONSE,
            packet_number=1,
            command=0x0E,
            session_id=0x11223344,
            sequence_number=9,
            acknowledge_number=3,
            payload=b'payload-data',
        )

        encoded = encode_messages([message])
        decoded = decode_datagram(encoded, endpoint)

        self.assertEqual(len(decoded), 1)
        self.assertEqual(decoded[0].type_flags, message.type_flags)
        self.assertEqual(decoded[0].packet_number, message.packet_number)
        self.assertEqual(decoded[0].command, message.command)
        self.assertEqual(decoded[0].session_id, message.session_id)
        self.assertEqual(decoded[0].sequence_number, message.sequence_number)
        self.assertEqual(decoded[0].acknowledge_number, message.acknowledge_number)
        self.assertEqual(decoded[0].payload, message.payload)
        self.assertEqual(decoded[0].footer_bytes, FOOTER_BYTES)

    def test_decode_rejects_missing_footer(self) -> None:
        endpoint = Endpoint(host='127.0.0.1', port=1111)
        with self.assertRaises(PacketDecodeError):
            decode_datagram(b'\x00' * 20, endpoint)

    def test_decode_accepts_kage_footer_marker(self) -> None:
        endpoint = Endpoint(host='127.0.0.1', port=1112)
        payload = b'payload-data'
        size_word = (FLAG_CHANNEL_BITS | FLAG_RESPONSE) | (len(payload) + 16)
        datagram = (
            struct.pack('>2H3L', size_word, (1 << 8) | 0x0E, 0x11223344, 9, 3)
            + payload
            + FOOTER_BYTES_KAGE
        )

        decoded = decode_datagram(datagram, endpoint)

        self.assertEqual(len(decoded), 1)
        self.assertEqual(decoded[0].payload, payload)
        self.assertEqual(decoded[0].footer_bytes, FOOTER_BYTES_KAGE)
        self.assertEqual(detect_footer_bytes(datagram), FOOTER_BYTES_KAGE)

    def test_encode_can_use_kage_footer_marker(self) -> None:
        endpoint = Endpoint(host='127.0.0.1', port=1113)
        message = SnapMessage(
            endpoint=endpoint,
            type_flags=FLAG_CHANNEL_BITS | FLAG_RESPONSE,
            packet_number=1,
            command=0x0E,
            session_id=0x11223344,
            sequence_number=9,
            acknowledge_number=3,
            payload=b'payload-data',
        )

        encoded = encode_messages([message], footer_bytes=FOOTER_BYTES_KAGE)

        self.assertTrue(encoded.endswith(FOOTER_BYTES_KAGE))
        self.assertFalse(encoded.endswith(FOOTER_BYTES))

    def test_decode_multi_query_datagram_keeps_first_embedded_entry_only(self) -> None:
        endpoint = Endpoint(host='127.0.0.1', port=2222)
        first = SnapMessage(
            endpoint=endpoint,
            type_flags=FLAG_ROOM | FLAG_MULTI,
            packet_number=0,
            command=0x09,
            session_id=0x12345678,
            sequence_number=7,
            acknowledge_number=0,
            payload=b'\x80\x02',
            size_word_override=(FLAG_ROOM | FLAG_MULTI) | 0x0012,
        )
        second = SnapMessage(
            endpoint=endpoint,
            type_flags=FLAG_ROOM,
            packet_number=0,
            command=0x07,
            session_id=0x12345678,
            sequence_number=0,
            acknowledge_number=0,
            payload=b'',
        )

        encoded = encode_messages([first, second])
        decoded = decode_datagram(encoded, endpoint)

        self.assertEqual(len(decoded), 1)
        self.assertEqual(decoded[0].command, 0x09)
        self.assertEqual(decoded[0].payload, b'\x80\x02')

    def test_decode_multi_send_datagram_keeps_embedded_followup(self) -> None:
        endpoint = Endpoint(host='127.0.0.1', port=2223)
        first = SnapMessage(
            endpoint=endpoint,
            type_flags=FLAG_ROOM | FLAG_MULTI,
            packet_number=0,
            command=0x0F,
            session_id=0x12345678,
            sequence_number=7,
            acknowledge_number=0,
            payload=b'\x80\x02',
            size_word_override=(FLAG_ROOM | FLAG_MULTI) | 0x0012,
        )
        second = SnapMessage(
            endpoint=endpoint,
            type_flags=FLAG_ROOM,
            packet_number=0,
            command=0x07,
            session_id=0x12345678,
            sequence_number=0,
            acknowledge_number=0,
            payload=b'',
        )

        encoded = encode_messages([first, second])
        decoded = decode_datagram(encoded, endpoint)

        self.assertEqual(len(decoded), 2)
        self.assertEqual(decoded[0].command, 0x0F)
        self.assertEqual(decoded[0].payload, b'\x80\x02')
        self.assertFalse(decoded[0].embedded_in_multi)
        self.assertEqual(decoded[1].command, 0x07)
        self.assertTrue(decoded[1].embedded_in_multi)


if __name__ == '__main__':
    unittest.main()
