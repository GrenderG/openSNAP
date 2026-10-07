"""Bootstrap game-detection tests."""

import struct
import unittest

from opensnap.core.bootstrap import detect_game_identifier
from opensnap.plugins.registry import identify_snap_title
from opensnap.protocol.constants import FLAG_CHANNEL_BITS, FOOTER_BYTES, FOOTER_BYTES_KAGE
from opensnap.protocol.models import Endpoint, SnapMessage

# Auto Modellista US login payload captured in `opensnap-bootstrap.log`
# (title code 0xCAAD at offset 100, then client address, local port, base port).
AM_RELEASE_LOGIN_PAYLOAD = bytes.fromhex(
    '6d6d6d6d0a' + '00' * 35
    + '6d6d6d6d0a4063' + '65692d61757468' + '00' * 46
    + '0000caad' + '00000000' + 'c0000264' + '000007d0' + '000003e8'
)


class BootstrapGameDetectionTests(unittest.TestCase):
    """Bootstrap logins carry the title code each game passes to `kkLoginClient`."""

    def test_captured_auto_modellista_login_is_identified(self) -> None:
        self.assertEqual(len(AM_RELEASE_LOGIN_PAYLOAD), 120)
        message = _message(footer_bytes=FOOTER_BYTES, payload=AM_RELEASE_LOGIN_PAYLOAD)
        self.assertEqual(_detect(message), 'automodellista')

    def test_title_code_and_footer_select_each_game(self) -> None:
        cases = (
            (0xCAAD, FOOTER_BYTES, 'automodellista'),
            (0xCAAD, FOOTER_BYTES_KAGE, 'automodellista_beta1'),
            (0xCA03, FOOTER_BYTES, 'monsterhunter'),
            # MH EU shares the Monster Hunter server (cross-region).
            (0xCA0E, FOOTER_BYTES, 'monsterhunter'),
            # Resident Evil Outbreak NA, v1 and v2 (`netwk.bin` `0x00584930`).
            (0xCAE0, FOOTER_BYTES, 'outbreak'),
        )
        for title_code, footer, expected in cases:
            message = _message(footer_bytes=footer, payload=_login_payload(title_code))
            self.assertEqual(_detect(message), expected, msg=hex(title_code))

    def test_bootstrap_games_choose_between_builds_sharing_a_title_code(self) -> None:
        # The MH NA public beta logs in with the release's 0xCA03; its own bootstrap
        # (`snap01.reo`, shared with Outbreak) serves it instead of the release.
        message = _message(footer_bytes=FOOTER_BYTES, payload=_login_payload(0xCA03))
        self.assertEqual(_detect(message, ('monsterhunter_na_beta', 'outbreak')), 'monsterhunter_na_beta')
        self.assertEqual(_detect(message, ('monsterhunter',)), 'monsterhunter')
        with self.assertLogs('opensnap.core.bootstrap', 'WARNING'):
            self.assertIsNone(_detect(message, ('outbreak',)))

    def test_unknown_title_code_is_not_identified(self) -> None:
        message = _message(footer_bytes=FOOTER_BYTES, payload=_login_payload(0x1234))
        with self.assertLogs('opensnap.core.bootstrap', 'WARNING') as captured:
            self.assertIsNone(_detect(message))
        self.assertIn('SN@P title 0x1234 (footer 0xba476611) is unknown or not served here', captured.output[0])

    def test_login_without_title_code_is_not_identified(self) -> None:
        for footer in (FOOTER_BYTES, FOOTER_BYTES_KAGE):
            with self.assertLogs('opensnap.core.bootstrap', 'WARNING'):
                self.assertIsNone(_detect(_message(footer_bytes=footer)))


def _detect(message: SnapMessage, games: tuple[str, ...] = ()) -> str | None:
    return detect_game_identifier(message=message, identify_snap_title=identify_snap_title, games=games)


def _login_payload(title_code: int) -> bytes:
    """`kkLoginClient` layout: login (40), auth string (60), title code, zero, address, ports."""

    login = b'test\n'.ljust(40, b'\x00') + b'test\n@cei-auth'.ljust(60, b'\x00')
    return login + struct.pack('>5L', title_code, 0, 0xC0000264, 2000, 1000)


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
