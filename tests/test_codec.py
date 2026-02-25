"""Protocol codec tests."""

import unittest

from opensnap.protocol.codec import PacketDecodeError, decode_datagram, encode_messages
from opensnap.protocol.constants import CHANNEL_LOBBY, FLAG_RESPONSE
from opensnap.protocol.models import Endpoint, SnapMessage


class CodecTests(unittest.TestCase):
    """Datagram encoding and decoding tests."""

    def test_round_trip_single_message(self) -> None:
        endpoint = Endpoint(host='127.0.0.1', port=12345)
        message = SnapMessage(
            endpoint=endpoint,
            type_flags=CHANNEL_LOBBY | FLAG_RESPONSE,
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

    def test_decode_rejects_missing_footer(self) -> None:
        endpoint = Endpoint(host='127.0.0.1', port=1111)
        with self.assertRaises(PacketDecodeError):
            decode_datagram(b'\x00' * 20, endpoint)


if __name__ == '__main__':
    unittest.main()
