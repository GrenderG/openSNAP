"""Bootstrap game-detection tests."""

import unittest

from opensnap.core.bootstrap import detect_game_identifier
from opensnap.protocol.constants import FLAG_CHANNEL_BITS, FOOTER_BYTES, FOOTER_BYTES_KAGE
from opensnap.protocol.models import Endpoint, SnapMessage


class BootstrapGameDetectionTests(unittest.TestCase):
    """Verify bootstrap game detection uses only proven bootstrap markers."""

    def test_detector_uses_configured_default_for_standard_footer(self) -> None:
        message = _message(footer_bytes=FOOTER_BYTES)

        detected = detect_game_identifier(
            message=message,
            default_game_identifier='automodellista',
        )

        self.assertEqual(detected, 'automodellista')

    def test_detector_keeps_configured_default_for_kage_footer(self) -> None:
        message = _message(footer_bytes=FOOTER_BYTES_KAGE)

        detected = detect_game_identifier(
            message=message,
            default_game_identifier='automodellista',
        )

        self.assertEqual(detected, 'automodellista')

    def test_detector_does_not_guess_from_primary_footer_cei_auth_shape(self) -> None:
        message = _message(
            footer_bytes=FOOTER_BYTES,
            payload=(
                b'test\n\x00'
                + (b'\x00' * 35)
                + b'test\n@cei-auth\x00'
                + (b'\x00' * 64)
            ),
        )

        detected = detect_game_identifier(
            message=message,
            default_game_identifier='monsterhunter',
        )

        self.assertEqual(detected, 'monsterhunter')

def _message(*, footer_bytes: bytes, payload: bytes = b'test\n\x00') -> SnapMessage:
    """Build a minimal bootstrap login packet."""

    return SnapMessage(
        endpoint=Endpoint(host='127.0.0.1', port=50000),
        type_flags=FLAG_CHANNEL_BITS,
        packet_number=0,
        command=0x2C,
        session_id=0,
        sequence_number=0,
        acknowledge_number=0,
        payload=payload,
        footer_bytes=footer_bytes,
    )


if __name__ == '__main__':
    unittest.main()
