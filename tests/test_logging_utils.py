"""Logging utility tests."""

import logging
import os
from pathlib import Path
import tempfile
import unittest

from opensnap.logging_utils import (
    DEFAULT_HEXDUMP_LIMIT,
    DEFAULT_LOG_BACKUP_COUNT,
    DEFAULT_LOG_MAX_MB,
    configure_logging,
    format_hexdump,
    parse_hexdump_limit,
    parse_log_backup_count,
    parse_log_level,
    parse_log_max_mb,
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

    def test_configure_logging_writes_service_log_in_configured_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            log_dir = Path(temp_dir) / 'logs'
            log_path = log_dir / 'opensnap-game-outbreak.log'
            os.environ['OPENSNAP_LOG_PATH'] = str(log_dir)
            try:
                configure_logging('info', service_name='game', instance_name='outbreak')
                logger = logging.getLogger('opensnap.test')
                logger.info('file-output-check')
                for handler in logging.getLogger().handlers:
                    handler.flush()
                self.assertTrue(log_path.exists())
                self.assertIn('file-output-check', log_path.read_text(encoding='utf-8'))
            finally:
                os.environ.pop('OPENSNAP_LOG_PATH', None)
                configure_logging('info', service_name='game')

    def test_configure_logging_rotates_at_configured_size(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / 'opensnap-web.log'
            os.environ.update(
                OPENSNAP_LOG_PATH=temp_dir,
                OPENSNAP_LOG_MAX_MB='1',
                OPENSNAP_LOG_BACKUP_COUNT='2',
            )
            try:
                configure_logging('info', service_name='web')
                logger = logging.getLogger('opensnap.test')
                for _ in range(4 * 1024):
                    logger.info('x' * 1024)
                for handler in logging.getLogger().handlers:
                    handler.flush()
                files = sorted(path.name for path in Path(temp_dir).iterdir())
                self.assertEqual(files, ['opensnap-web.log', 'opensnap-web.log.1', 'opensnap-web.log.2'])
                self.assertLessEqual(log_path.stat().st_size, 1024 * 1024)
            finally:
                for key in ('OPENSNAP_LOG_PATH', 'OPENSNAP_LOG_MAX_MB', 'OPENSNAP_LOG_BACKUP_COUNT'):
                    os.environ.pop(key, None)
                configure_logging('info', service_name='web')

    def test_parse_log_rotation_settings_fall_back_on_invalid_values(self) -> None:
        self.assertEqual(parse_log_max_mb('abc'), DEFAULT_LOG_MAX_MB)
        self.assertEqual(parse_log_max_mb('-5'), DEFAULT_LOG_MAX_MB)
        self.assertEqual(parse_log_max_mb('0'), 0)
        self.assertEqual(parse_log_backup_count('0'), DEFAULT_LOG_BACKUP_COUNT)
        self.assertEqual(parse_log_backup_count('3'), 3)


if __name__ == '__main__':
    unittest.main()
