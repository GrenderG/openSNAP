"""Logging utility tests."""

import logging
import unittest

from opensnap.logging_utils import (
    DEFAULT_HEXDUMP_LIMIT,
    format_hexdump,
    parse_hexdump_limit,
    parse_log_level,
)


class LoggingUtilsTests(unittest.TestCase):
    """Validate logging helpers and hexdump formatting."""

    def test_parse_log_level_supports_warn_alias(self) -> None:
        self.assertEqual(parse_log_level('warn'), logging.WARNING)
        self.assertEqual(parse_log_level('warning'), logging.WARNING)
        self.assertEqual(parse_log_level('DEBUG'), logging.DEBUG)

    def test_parse_log_level_defaults_to_info_for_unknown_value(self) -> None:
        self.assertEqual(parse_log_level('unknown-level'), logging.INFO)

    def test_parse_hexdump_limit_falls_back_on_invalid_value(self) -> None:
        self.assertEqual(parse_hexdump_limit('abc'), DEFAULT_HEXDUMP_LIMIT)
        self.assertEqual(parse_hexdump_limit('0'), 0)
        self.assertEqual(parse_hexdump_limit('-1'), DEFAULT_HEXDUMP_LIMIT)

    def test_format_hexdump_includes_offsets_ascii_and_truncation(self) -> None:
        payload = b'abcdef0123456789XYZ'
        dump = format_hexdump(payload, max_bytes=8)
        self.assertIn('0000', dump)
        self.assertIn('61 62 63 64 65 66 30 31', dump)
        self.assertIn('abcdef01', dump)
        self.assertIn('truncated', dump)

    def test_format_hexdump_unlimited_when_limit_is_zero(self) -> None:
        payload = b'abcdef0123456789XYZ'
        dump = format_hexdump(payload, max_bytes=0)
        self.assertNotIn('truncated', dump)


if __name__ == '__main__':
    unittest.main()
