"""Resident Evil Outbreak APP TCP service.

The client (`SLUS_207.65`, v1 and v2) opens three kinds of APP connection to
`app01.reo.capcom.sf.yav4.com:10127` (`0x001f68d4`):

- mode 2 from the browser for `lbs://` pages (the DATABASE menu, `database.py`):
  6101/6102, 1004;
- mode 1 on each lobby entry (`netwk.bin` `0x00608f54`, `0x00609408`):
  6101/6102 information page, 6401 notice pages, 6201/6202 data files
  (`netbio.py`), 1004;
- mode 0 after a scenario (`netwk.bin` `0x00584988`): 6301 result report
  (`results.py`), 1004.
  A failure shows `ERR:D9xx` (`0x0058458c`), e.g. D906 when the socket breaks
  while the client waits for a reply.

Both modes wait for the server to speak first, so every connection opens the
same way: the APP endpoint's 1002 (the client answers 1002 and goes to step 1
in mode 1, or sends its 6301 in mode 0, `0x001f4f54`), then this service's
6001 without a patch (mode 1 asks for its page, mode 0 ignores it,
`0x001f50f0`). Client frames are dispatched by
direction and command only (`0x001f6cf4`). Addresses are v2; v1's main ELF is
`-0x910`.
"""

from dataclasses import dataclass
import logging
import os
from pathlib import Path
import socket
import struct

from opensnap.storage.interfaces import AccountStore, RecordStore
from opensnap_app.capcom.connection import AppConnection
from opensnap_app.capcom.outbreak.database import DatabasePages
from opensnap_app.capcom.outbreak.netbio import NETBIO_NAME, netbio_files
from opensnap_app.capcom.outbreak.results import decode_scenario_result, store_scenario_result
from opensnap_app.capcom.protocol import (
    APP_DIRECTION_PUSH,
    CMD_APP_COMPLETE,
    CMD_APP_VERSION,
    VERSION_NO_PATCH,
    ConnectAnswer,
    app_frame,
    decode_app_field,
    encode_app_field,
    frame_command,
    frame_payload,
    frame_sequence,
)

_LOGGER = logging.getLogger('opensnap_app.capcom.outbreak')

DEFAULT_DATA_DIRECTORY = 'data'
DATA_SUBDIRECTORY = 'outbreak'
FILES_DIRECTORY = 'files'
# The client fails a connection after 1800 polls without a frame (about 30 s).
CONNECTION_TIMEOUT_SECONDS = 30.0
CLOSE_LINGER_SECONDS = 2.0

# Build byte of the client's 1002 answer (`netwk.bin` `0x00608f0c`, `nethttp.bin`
# `0x0073482c`); the same in v1 and v2.
APP_BUILD = 1

CMD_APP_PAGE_INFO = 0x6101
CMD_APP_PAGE_CHUNK = 0x6102
CMD_APP_DATA_FILES = 0x6201
CMD_APP_DATA_CHUNK = 0x6202
CMD_APP_RESULT_REPORT = 0x6301
CMD_APP_NOTICE_PAGES = 0x6401

# 6101 `u32, field page` -> `u8 flag, u32, field page, u32 size` (`0x001f60b0`):
# flag 0 fails the connection (-62), the page name must match (-61), and size
# 0 skips to a step openSNAP does not serve, so a page is always published:
# the client asks for `01/TOP_INFOR.HTM` (main `0x00259b20`), served from
# `files/` like Monster Hunter's, and a missing page is published empty.
# 6102 `field page, u32 offset, u16 chunk` -> `field page, u32 offset, field
# data` (`0x001f6420`): the offset must be the client's running offset (-60)
# and the data is exactly the requested chunk, cut at the page end. The client
# keeps at most 4096 bytes and does not terminate the page itself. Browser
# (`lbs://`) pages may be up to 0x8000 bytes (`nethttp.bin` `0x00734870`); the
# DATABASE pages stay below 4096 too, so one limit serves both.
PAGE_FOUND = b'\x01'
PAGE_LIMIT = 4096
# 6401 -> `u16 count, count x u32 size`; 0 keeps the client's built-in DNAS and
# SCEA notice pages (`0x001f54f0`).
NO_NOTICE_PAGES = b'\x00\x00'
# 6201 `field` -> `u8 flag, field name, u16 count, count x u32 size`; flag 0
# would mean no files (`0x001f59c0`). 6202 `u16 file, u32 offset, u16 chunk`
# -> `u16 file, u32 offset, field data`, the client asking for 754-byte chunks
# in order (`0x001f5b50`).
DATA_FILES_FOUND = b'\x01'
# 6301 -> empty reply with error 0; error `0xFF` fails the upload (-59,
# `0x001f67d0`).
RESULT_REPORT_RECEIVED = b''


@dataclass(frozen=True, slots=True)
class AppServiceConfig:
    """Data folder of the APP service."""

    data_directory: Path


def read_app_service_config() -> AppServiceConfig:
    """Build APP settings from the environment."""

    data_root = os.getenv('OPENSNAP_DATA_DIR', '').strip() or DEFAULT_DATA_DIRECTORY
    return AppServiceConfig(data_directory=Path(data_root) / DATA_SUBDIRECTORY)


class AppService:
    """Serve every Outbreak APP connection."""

    def __init__(self, config: AppServiceConfig, accounts: AccountStore, records: RecordStore) -> None:
        self._files_root = config.data_directory / FILES_DIRECTORY
        self._accounts = accounts
        self._records = records
        self._database = DatabasePages(records)
        self._data_files = netbio_files()

    def serve(self, connection: AppConnection, answer: ConnectAnswer) -> None:
        """Offer no patch, then answer the client's requests until its 1004."""

        login = answer.login.split(b'\x00', 1)[0].decode('ascii', errors='replace')
        _LOGGER.info('APP client %r from %s.', login, connection.host)
        # Pages as published by this connection's 6101, so its 6102 chunks match.
        pages: dict[bytes, bytes] = {}
        try:
            connection.set_timeout(CONNECTION_TIMEOUT_SECONDS)
            connection.send(app_frame(APP_DIRECTION_PUSH, CMD_APP_VERSION, payload=VERSION_NO_PATCH))
            while True:
                request = connection.receive()
                command = frame_command(request)
                if command == CMD_APP_COMPLETE:
                    connection.reply(request)
                    connection.linger(CLOSE_LINGER_SECONDS)
                    return
                reply = self._reply(request, login, pages)
                if reply is None:
                    _LOGGER.warning('Unexpected APP command 0x%04x from %s; not answered.', command, connection.host)
                    continue
                connection.reply(request, reply)
        except (ConnectionError, socket.timeout, ValueError) as exc:
            _LOGGER.info('APP connection from %s:%d closed: %s', connection.host, connection.port, exc)

    def _reply(self, request: bytes, login: str, pages: dict[bytes, bytes]) -> bytes | None:
        command = frame_command(request)
        sequence = frame_sequence(request)
        payload = frame_payload(request)
        if command == CMD_APP_PAGE_INFO:
            if len(payload) < 4:
                raise ValueError('page request is truncated')
            name, _ = decode_app_field(payload[4:], sequence)
            page = pages[name] = self._page(name)
            return PAGE_FOUND + bytes(4) + encode_app_field(name, sequence) + struct.pack('>L', len(page))
        if command == CMD_APP_PAGE_CHUNK:
            name, size = decode_app_field(payload, sequence)
            if len(payload) != size + 6:
                raise ValueError('page chunk request has an invalid size')
            offset, chunk = struct.unpack_from('>LH', payload, size)
            page = pages.get(name) or self._page(name)
            if offset >= len(page):
                raise ValueError('page chunk request is past the page end')
            return (
                encode_app_field(name, sequence)
                + struct.pack('>L', offset)
                + encode_app_field(page[offset:offset + chunk], sequence)
            )
        if command == CMD_APP_NOTICE_PAGES:
            return NO_NOTICE_PAGES
        if command == CMD_APP_DATA_FILES:
            return (
                DATA_FILES_FOUND
                + encode_app_field(NETBIO_NAME, sequence)
                + struct.pack(f'>H{len(self._data_files)}L', len(self._data_files), *map(len, self._data_files))
            )
        if command == CMD_APP_DATA_CHUNK:
            if len(payload) != 8:
                raise ValueError('data file chunk request has an invalid size')
            index, offset, chunk = struct.unpack('>HLH', payload)
            if index >= len(self._data_files) or offset >= len(self._data_files[index]):
                raise ValueError('data file chunk request is past the files')
            data = self._data_files[index][offset:offset + chunk]
            return struct.pack('>HL', index, offset) + encode_app_field(data, sequence)
        if command == CMD_APP_RESULT_REPORT:
            self._store_result(payload, sequence, login)
            return RESULT_REPORT_RECEIVED
        return None

    def _store_result(self, payload: bytes, sequence: int, login: str) -> None:
        result = decode_scenario_result(payload, sequence)
        account = self._accounts.get_by_name(login)
        if account is None:
            _LOGGER.warning('Scenario result from unknown login %r; not stored.', login)
            return
        store_scenario_result(self._records, account, result)
        _LOGGER.info(
            'Scenario result from %s stored: scenario %d (%s mode), cleared %s, %d ticks, %d points.',
            account.username, result.scenario, 'free' if result.free_mode else 'scenario', result.cleared,
            result.clear_ticks, result.points,
        )

    def _page(self, name: bytes) -> bytes:
        """A DATABASE page, else the named page from `files/` (empty when missing), NUL-terminated."""

        text = name.split(b'\x00', 1)[0].decode('ascii', errors='replace')
        content = self._database.render(text)
        if content is None:
            root = self._files_root.resolve()
            path = (root / text).resolve()
            content = path.read_bytes() if root in path.parents and path.is_file() else b''
        return content[:PAGE_LIMIT - 1] + b'\x00'
