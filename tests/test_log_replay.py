"""Replay regression tests from captured snapsi logs."""

from dataclasses import replace
from pathlib import Path
import re
import tempfile
import unittest

from opensnap.config import StorageConfig, default_app_config
from opensnap.core.engine import SnapProtocolEngine
from opensnap.plugins.automodellista import AutoModellistaPlugin
from opensnap.protocol.models import Endpoint

PACKET_HEADER_PATTERN = re.compile(r'^packet received from ([0-9.]+):([0-9]+)$')
HEX_LINE_PATTERN = re.compile(r'^[0-9a-fA-F]{8}\s+(.*?)\s+\|')


class LogReplayTests(unittest.TestCase):
    """Replay captured datagrams through the protocol engine."""

    def test_replay_logs_on_default_sqlite_backend(self) -> None:
        with tempfile.TemporaryDirectory() as temp_directory:
            config = replace(
                default_app_config(),
                storage=StorageConfig(
                    backend='sqlite',
                    sqlite_path=f'{temp_directory}/replay-default.sqlite',
                ),
            )
            engine = SnapProtocolEngine(config=config, plugin=AutoModellistaPlugin())
            datagrams = _load_captured_datagrams()
            if not datagrams:
                self.skipTest('Replay logs are unavailable.')
            self.assertGreater(len(datagrams), 100)

            errors = _replay(engine, datagrams)
            self.assertFalse(errors, '\n'.join(errors[:10]))

    def test_replay_logs_on_sqlite_backend(self) -> None:
        with tempfile.TemporaryDirectory() as temp_directory:
            config = replace(
                default_app_config(),
                storage=StorageConfig(
                    backend='sqlite',
                    sqlite_path=f'{temp_directory}/replay.sqlite',
                ),
            )
            engine = SnapProtocolEngine(config=config, plugin=AutoModellistaPlugin())
            datagrams = _load_captured_datagrams(limit_per_file=100)
            if not datagrams:
                self.skipTest('Replay logs are unavailable.')
            self.assertGreater(len(datagrams), 50)

            errors = _replay(engine, datagrams)
            self.assertFalse(errors, '\n'.join(errors[:10]))


def _replay(engine: SnapProtocolEngine, datagrams: list[tuple[Endpoint, bytes]]) -> list[str]:
    """Replay datagrams and collect handler errors."""

    errors: list[str] = []
    for endpoint, payload in datagrams:
        result = engine.handle_datagram(payload, endpoint)
        errors.extend(result.errors)
    return errors


def _load_captured_datagrams(limit_per_file: int | None = None) -> list[tuple[Endpoint, bytes]]:
    """Load `packet received` datagrams from snapsi log files."""

    root = Path(__file__).resolve().parents[1]
    log_directory = root / 'snapsi' / 'src' / 'logs'
    if not log_directory.exists():
        return []
    datagrams: list[tuple[Endpoint, bytes]] = []

    for log_path in sorted(log_directory.glob('*.txt')):
        file_datagrams = _extract_file_datagrams(log_path)
        if limit_per_file is not None:
            file_datagrams = file_datagrams[:limit_per_file]
        datagrams.extend(file_datagrams)
    return datagrams


def _extract_file_datagrams(log_path: Path) -> list[tuple[Endpoint, bytes]]:
    """Parse one log file and extract received datagrams."""

    datagrams: list[tuple[Endpoint, bytes]] = []
    endpoint: Endpoint | None = None
    payload = bytearray()

    for raw_line in log_path.read_text(encoding='utf-8', errors='ignore').splitlines():
        line = raw_line.rstrip()
        packet_match = PACKET_HEADER_PATTERN.match(line)
        if packet_match:
            _flush_datagram(datagrams, endpoint, payload)
            endpoint = Endpoint(host=packet_match.group(1), port=int(packet_match.group(2)))
            payload = bytearray()
            continue

        hex_match = HEX_LINE_PATTERN.match(line)
        if endpoint is not None and hex_match:
            hex_part = hex_match.group(1).replace('-', ' ')
            for token in hex_part.split():
                if len(token) == 2:
                    payload.append(int(token, 16))
            continue

        if endpoint is not None and payload:
            _flush_datagram(datagrams, endpoint, payload)
            endpoint = None
            payload = bytearray()

    _flush_datagram(datagrams, endpoint, payload)
    return datagrams


def _flush_datagram(
    datagrams: list[tuple[Endpoint, bytes]],
    endpoint: Endpoint | None,
    payload: bytearray,
) -> None:
    """Append current datagram if populated."""

    if endpoint is None or not payload:
        return
    datagrams.append((endpoint, bytes(payload)))


if __name__ == '__main__':
    unittest.main()
