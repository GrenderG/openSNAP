"""Resident Evil Outbreak File#2 APP TCP service.

The File#2 client (`SLUS_209.84` NA, `SLES_533.19` EU) runs the Monster
Hunter family of the Capcom APP client (NA `0x001f7b90..0x001fef40`, EU
`0x001c2da0..0x001ca570`), not File#1's. It answers the server's 1002 with
build 4 (NA, `0x001f8fd8`) or 6 (EU, `0x001c41f4`), waits for the 6001 patch
offer, and then drives one of seven connection kinds (`tasks/re_outbreak2_app.md`
§5). Addresses are NA unless marked EU.

- Information (account menu, `netwk.bin` `0x00618ca8`): 6110, EU 6220, 6210,
  6101/6102 information page, 6401, 6403, 6105, 1004. A repeat (mode 7) skips
  6401 and 6403.
- Login (`0x0061a2a4`): 1007, 6103, 6212, 6203/6204 data files, 6211, 6213,
  6501, 6503, 6502, 6504, 6510, 1004. The 6503 World key is the host the
  lobby then resolves and logs into (`netwk.bin` `0x005b5cf0`, EU
  `0x005c7a00`), the 6504 Land key is the prefix of the Area names it asks
  SN@P for, and the 6504 capacity is the "Server full" limit (`0x0059c730`).
- Population refresh (`0x005b5c58`): 6501 (flag 0), 6510, 1004.
- Data refresh (`0x00607ee4`): 6203/6204, 6211, 1004.
- Scenario result (`0x005b51d8`): 6203/6204, 6211, 6301 report, 1004.
- Browser `lbs://` page (`nethttp.bin` `0x0074181c`): 6101/6102, 1004.

Every request is answered from its command alone, so one loop serves all of
them. A connection fails with `ERR:D9xx` on a wrong reply (`0x0059ce48`).
"""

from collections import Counter
from dataclasses import dataclass
import logging
import os
from pathlib import Path
import socket
import struct

from opensnap.config import AppConfig
from opensnap.core.bootstrap.handlers import _resolve_advertise_host
from opensnap.plugins.outbreak2.directory import AREA_COUNT, Directory
from opensnap.plugins.outbreak2.records import GAME
from opensnap.storage.interfaces import AccountStore, OnlinePlayerStore, RecordStore
from opensnap_app.capcom.connection import AppConnection
from opensnap_app.capcom.outbreak2.database import DatabasePages
from opensnap_app.capcom.outbreak2.netbio import data_files, data_token
from opensnap_app.capcom.outbreak2.results import store_scenario_result
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
from opensnap_app.capcom.scenario_report import decode_scenario_result

_LOGGER = logging.getLogger('opensnap_app.capcom.outbreak2')

DEFAULT_DATA_DIRECTORY = 'data'
DATA_SUBDIRECTORY = 'outbreak2'
FILES_DIRECTORY = 'files'
# The client fails a connection after 1800 polls without a frame (`0x001fe61c`).
CONNECTION_TIMEOUT_SECONDS = 30.0
CLOSE_LINGER_SECONDS = 2.0

# Build bytes of the 1002 answer; EU's flags byte is `language | 0x10`
# (`0x001c4204`) and picks its information page directory.
APP_BUILD_NA = 4
APP_BUILD_EU = 6
APP_LANGUAGE_MASK = 0x0F

CMD_APP_LOGIN = 0x1007
CMD_APP_PAGE_INFO = 0x6101
CMD_APP_PAGE_CHUNK = 0x6102
CMD_APP_LOGIN_PAGES = 0x6103
CMD_APP_LOGIN_FILES = 0x6105
CMD_APP_NETWORK_SETTINGS = 0x6110
CMD_APP_DATA_FILES = 0x6203
CMD_APP_DATA_CHUNK = 0x6204
CMD_APP_NET_TUNING = 0x6210
CMD_APP_DATA_VERSION = 0x6211
CMD_APP_LOGIN_WORD = 0x6212
CMD_APP_CONNECTION_TIMING = 0x6213
CMD_APP_AUTH_SUFFIX = 0x6220
CMD_APP_RESULT_REPORT = 0x6301
CMD_APP_NOTICE_PAGES = 0x6401
CMD_APP_AGREEMENT_PAGES = 0x6403
CMD_APP_SERVER_ADDRESS = 0x6501
CMD_APP_WORLD_STATUS = 0x6502
CMD_APP_WORLD_LIST = 0x6503
CMD_APP_LAND_LIST = 0x6504
CMD_APP_LAND_POPULATION = 0x6510

# 6110 reply (`0x001fb620`): `u16 port parameter` (0 keeps 100), `field regweb
# URL`, `field bootstrap host`; empty fields keep the client defaults. The
# registration menu opens the regweb URL, whose default is a Monster Hunter
# leftover (`http://stage6.sega.com/mhweb/index.jsp?DNAS=`, `0x002696b0`), so
# the reply points it at the Outbreak signup page served by the web service.
PORT_PARAMETER_DEFAULT = struct.pack('>H', 0)
REGISTRATION_URL = b'http://regweb.reo.capcom.sf.yav4.com/reweb/signup/'
# EU 6220 reply (`0x001c6a50`): `u8 1, u32 0` keeps the `@cee-auth` login.
AUTH_SUFFIX_DEFAULT = b'\x01' + bytes(4)
# 6210 reply (`0x001f9510`): 17 words; zeros keep the in-game timers (31.0 s
# peer timeout, 0.5 s reliable flush).
NET_TUNING_DEFAULTS = bytes(17 * 4)
# 6213 reply (`0x001f9230`): eight frame counts, the client's own defaults
# (`0x001f9250`). Words 4/5 are the room search limits, 6/7 the lobby
# keepalive idle and timeout frames.
CONNECTION_TIMING_FRAMES = (20, 300, 300, 5400, 1800, 1800, 1800, 1800)
# 1007 reply flag 0 (`0x001fda00`); 6103/6105/6401/6403 `u16 0`: nothing to
# download, the client keeps its built-in pages (`0x001f9e20`, `0x001fa2f0`);
# 6211/6212 words have no reader (`0x001f93d0`, `0x001f9140`).
LOGIN_NO_DATA = b'\x00'
NO_PAGES = b'\x00\x00'
ZERO_WORD = bytes(4)

# 6101 `u32, field page` -> `u8 1, u32 0, field page, u32 size` (`0x001fa800`;
# flag 0 fails with -62, a name mismatch with -61). 6102 `field page, u32
# offset, u16 chunk` -> `field page, u32 offset, field data` (`0x001faa70`),
# the chunk being 722 and the offset the client's running offset. The client
# cuts pages at 4096 bytes (information) or 0x8000 (`lbs://`, `0x001fab64`)
# and the lobby copies the information page into a zeroed 4096-byte buffer
# (`0x00618d10`), so a page ends with a NUL within its limit.
PAGE_FOUND = b'\x01' + bytes(4)
INFORMATION_PAGE_LIMIT = 4096
BROWSER_PAGE_LIMIT = 0x8000
# 6203 `field token` -> `u8 1, field name, u16 count, count x u32 size`
# (`0x001f9740`): a name equal to the client's token skips the download, so
# every client gets the same token and only a changed file changes it. 6204
# `u16 file, u32 offset, u16 chunk` -> `u16 file, u32 offset, field data`.
DATA_FILES_FOUND = b'\x01'
# 6501 `u32 token` -> `u8 1, u32 refresh delay, u32 token` loads the server
# list (`0x001fb850`): the lobby counts the first word down before it allows a
# population refresh (`0x005b5c4c`, `0x00616a28`) and the refresh sends the
# second word back (`0x001fb7d4`); the login sends 0 (`0x001fb7cc`). A refresh
# therefore carries the token 1 and gets flag 0, which goes to 6510 alone.
# 6502 `u32, field key` -> `u8 1, u32, u32` (`0x001fc050`), neither word read.
SERVER_LIST_TOKEN = 1
SERVER_LIST_FOLLOWS = b'\x01' + struct.pack('>2L', 0, SERVER_LIST_TOKEN)
POPULATIONS_ONLY = b'\x00'
WORLD_STATUS_OK = b'\x01' + bytes(8)
# 6301 -> empty reply with error 0; error `0xFF` fails the upload (-59).
RESULT_REPORT_RECEIVED = b''


@dataclass(frozen=True, slots=True)
class AppServiceConfig:
    """Data folder of the APP service and the bootstrap host it sends the game to."""

    data_directory: Path
    # The bootstrap service as `OPENSNAP_BOOTSTRAP_HOST` and
    # `OPENSNAP_BOOTSTRAP_ADVERTISE_HOST` give it; `0.0.0.0` or an empty
    # advertised host means this machine, by its address towards each client.
    bootstrap_host: str = '0.0.0.0'
    bootstrap_advertise_host: str = ''


def read_app_service_config(config: AppConfig) -> AppServiceConfig:
    """Build APP settings from the environment and the bootstrap endpoint settings."""

    data_root = os.getenv('OPENSNAP_DATA_DIR', '').strip() or DEFAULT_DATA_DIRECTORY
    return AppServiceConfig(
        data_directory=Path(data_root) / DATA_SUBDIRECTORY,
        bootstrap_host=config.server.bootstrap.host,
        bootstrap_advertise_host=config.server.bootstrap.advertise_host,
    )


class AppService:
    """Serve every File#2 APP connection."""

    def __init__(
        self,
        config: AppServiceConfig,
        directory: Directory,
        accounts: AccountStore,
        records: RecordStore,
        online_players: OnlinePlayerStore,
    ) -> None:
        self._files_root = config.data_directory / FILES_DIRECTORY
        self._bootstrap_host = config.bootstrap_host
        self._bootstrap_advertise_host = config.bootstrap_advertise_host
        self._directory = directory
        self._accounts = accounts
        self._records = records
        self._online_players = online_players
        self._database = DatabasePages(records, directory)
        self._data_files = data_files(directory)
        self._data_token = data_token(directory)

    def serve(self, connection: AppConnection, answer: ConnectAnswer) -> None:
        """Offer no patch, then answer the client's requests until its 1004."""

        login = answer.login.split(b'\x00', 1)[0].decode('ascii', errors='replace')
        language = answer.flags & APP_LANGUAGE_MASK if answer.build == APP_BUILD_EU else 0
        _LOGGER.info('APP client %r (build %d, language %d) from %s.', login, answer.build, language, connection.host)
        # Pages as published by this connection's 6101, so its 6102 chunks match.
        pages: dict[bytes, bytes] = {}
        # The bootstrap address this client can reach, as a dotted IP: the
        # lobby resolves the World key with the PS2 resolver (`0x001f4cd0`),
        # which takes a dotted IP as is (`0x001f4a80`) and would otherwise
        # look a name up through the player's DNS.
        bootstrap_host = _resolve_advertise_host(
            configured_host=self._bootstrap_advertise_host,
            bind_host=self._bootstrap_host,
            client_host=connection.host,
        )
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
                reply = self._reply(request, login, pages, bootstrap_host)
                if reply is None:
                    _LOGGER.warning('Unexpected APP command 0x%04x from %s; not answered.', command, connection.host)
                    continue
                connection.reply(request, reply)
        except (ConnectionError, socket.timeout, ValueError) as exc:
            _LOGGER.info('APP connection from %s:%d closed: %s', connection.host, connection.port, exc)

    def _reply(self, request: bytes, login: str, pages: dict[bytes, bytes], bootstrap_host: str) -> bytes | None:
        command = frame_command(request)
        sequence = frame_sequence(request)
        payload = frame_payload(request)

        def encode_field(data: bytes) -> bytes:
            return encode_app_field(data, sequence)

        if command == CMD_APP_NETWORK_SETTINGS:
            return PORT_PARAMETER_DEFAULT + encode_field(REGISTRATION_URL) + encode_field(b'')
        if command == CMD_APP_AUTH_SUFFIX:
            return AUTH_SUFFIX_DEFAULT
        if command == CMD_APP_NET_TUNING:
            return NET_TUNING_DEFAULTS
        if command == CMD_APP_PAGE_INFO:
            if len(payload) < 4:
                raise ValueError('page request is truncated')
            name, _ = decode_app_field(payload[4:], sequence)
            page = pages[name] = self._page(name)
            return PAGE_FOUND + encode_field(name) + struct.pack('>L', len(page))
        if command == CMD_APP_PAGE_CHUNK:
            name, size = decode_app_field(payload, sequence)
            if len(payload) != size + 6:
                raise ValueError('page chunk request has an invalid size')
            offset, chunk = struct.unpack_from('>LH', payload, size)
            page = pages.get(name) or self._page(name)
            if offset >= len(page):
                raise ValueError('page chunk request is past the page end')
            return encode_field(name) + struct.pack('>L', offset) + encode_field(page[offset:offset + chunk])
        if command in (CMD_APP_LOGIN_PAGES, CMD_APP_LOGIN_FILES, CMD_APP_NOTICE_PAGES, CMD_APP_AGREEMENT_PAGES):
            return NO_PAGES
        if command == CMD_APP_LOGIN:
            return LOGIN_NO_DATA
        if command in (CMD_APP_DATA_VERSION, CMD_APP_LOGIN_WORD):
            return ZERO_WORD
        if command == CMD_APP_DATA_FILES:
            return (
                DATA_FILES_FOUND
                + encode_field(self._data_token)
                + struct.pack(f'>H{len(self._data_files)}L', len(self._data_files), *map(len, self._data_files))
            )
        if command == CMD_APP_DATA_CHUNK:
            if len(payload) != 8:
                raise ValueError('data file chunk request has an invalid size')
            index, offset, chunk = struct.unpack('>HLH', payload)
            if index >= len(self._data_files) or offset >= len(self._data_files[index]):
                raise ValueError('data file chunk request is past the files')
            data = self._data_files[index][offset:offset + chunk]
            return struct.pack('>HL', index, offset) + encode_field(data)
        if command == CMD_APP_CONNECTION_TIMING:
            return struct.pack('>8H', *CONNECTION_TIMING_FRAMES)
        if command == CMD_APP_SERVER_ADDRESS:
            return SERVER_LIST_FOLLOWS if payload == bytes(4) else POPULATIONS_ONLY
        if command == CMD_APP_WORLD_LIST:
            return self._world_list(payload, bootstrap_host, encode_field)
        if command == CMD_APP_WORLD_STATUS:
            return WORLD_STATUS_OK
        if command == CMD_APP_LAND_LIST:
            return self._land_list(payload, encode_field)
        if command == CMD_APP_LAND_POPULATION:
            return self._land_populations(payload, sequence)
        if command == CMD_APP_RESULT_REPORT:
            self._store_result(payload, sequence, login)
            return RESULT_REPORT_RECEIVED
        return None

    def _world_list(self, request: bytes, bootstrap_host: str, encode_field) -> bytes:
        """Answer 6503 `u16 start, u16 count`: this server as the only World.

        Reply (`0x001fba80`): `u16 total, u16 start, u8 count`, then per World
        key field (62), name field (15), 8 bytes, description field (255). The
        server list shows the name and description (`0x005b5ec0`). The key is
        the World's bootstrap host: on "Connect" the lobby resolves it
        (`netwk.bin` `0x005b5cf0`, EU `0x005c7a00`) and logs into that address
        (`0x00619eac`), so it names the World in the later requests as well.
        """

        if len(request) < 4:
            raise ValueError('World list request is truncated.')
        start, _ = struct.unpack_from('>2H', request)
        entry = (
            encode_field(bootstrap_host.encode('ascii'))
            + encode_field(self._directory.server_name.encode('ascii'))
            + bytes(8)
            + encode_field(self._directory.server_description.encode('ascii'))
        )
        return struct.pack('>2HB', 1, start, 0 if start else 1) + (b'' if start else entry)

    def _land_list(self, request: bytes, encode_field) -> bytes:
        """Answer 6504 `u16 start, u16 count, field World key`: one Land, the ten Areas.

        Reply (`0x001fc440`): `u16 total, u16 start, u8 count`, then per Land
        key field (15), name field (15), 8 bytes, description field (255),
        `u16 Area count, u16 capacity, u32`. The lobby names its Areas
        `<key>01`..`<key>10` and compares the Area populations with the
        capacity ("Server full.", `0x0059c730`).
        """

        if len(request) < 4:
            raise ValueError('Land list request is truncated.')
        start, _ = struct.unpack_from('>2H', request)
        entry = (
            encode_field(self._directory.area_key.encode('ascii'))
            + encode_field(self._directory.server_name.encode('ascii'))
            + bytes(8)
            + encode_field(self._directory.server_description.encode('ascii'))
            + struct.pack('>2HL', AREA_COUNT, self._directory.capacity, 0)
        )
        return struct.pack('>2HB', 1, start, 0 if start else 1) + (b'' if start else entry)

    def _land_populations(self, request: bytes, sequence: int) -> bytes:
        """Answer 6510 `u8 count, count x field key, field World key` (`0x001fcf90`).

        Reply (`0x001fd160`): `u8 count, count x u16 population, field World
        key`. The population is the number of players in the Areas, from the
        shared store the game server publishes.
        """

        if not request:
            raise ValueError('Land population request is empty.')
        offset = 1
        keys = []
        for _ in range(request[0]):
            key, size = decode_app_field(request[offset:], sequence)
            keys.append(key.rstrip(b'\x00').decode('ascii', errors='ignore'))
            offset += size
        world_field = request[offset:]
        if len(world_field) < 4 or 2 + int.from_bytes(world_field[:2], 'big') != len(world_field):
            raise ValueError('Land population request has an invalid world field.')
        in_areas = Counter(
            player.area_id for player in self._online_players.list(GAME) if player.area_id
        )
        populations = [sum(in_areas.values()) if key == self._directory.area_key else 0 for key in keys]
        return struct.pack(f'>B{len(populations)}H', len(populations), *populations) + world_field

    def _store_result(self, payload: bytes, sequence: int, login: str) -> None:
        result = decode_scenario_result(payload, sequence)
        account = self._accounts.get_by_name(login)
        if account is None:
            _LOGGER.warning('Scenario result from unknown login %r; not stored.', login)
            return
        store_scenario_result(self._records, account, result)
        _LOGGER.info(
            'Scenario result from %s stored: scenario %d (rank kind %d), cleared %s, %d ticks, %d points.',
            account.username, result.scenario, result.mode, result.cleared, result.clear_ticks, result.points,
        )

    def _page(self, name: bytes) -> bytes:
        """A DATABASE page, else the named page from `files/` (empty when missing), NUL-terminated."""

        text = name.split(b'\x00', 1)[0].decode('ascii', errors='replace')
        content = self._database.render(text)
        if content is not None:
            return content[:BROWSER_PAGE_LIMIT - 1] + b'\x00'
        root = self._files_root.resolve()
        path = (root / text).resolve()
        content = path.read_bytes() if root in path.parents and path.is_file() else b''
        return content[:INFORMATION_PAGE_LIMIT - 1] + b'\x00'
