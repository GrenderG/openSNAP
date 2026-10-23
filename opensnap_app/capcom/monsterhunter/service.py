"""Monster Hunter APP TCP service.

The client opens short TCP connections to the APP server around its SNAP
session: APP1 before bootstrap login, then WORLD, POSTWORLD, and LAND after
KICS login, plus LAND reconnects after quests. The Monster Hunter game server
publishes its logged-in players and their Areas in the shared store
(`OnlinePlayerStore`); this service reads them to follow each player's
connections and to count Land populations. The exchanges below reproduce
the runtime-validated server responses of the MH1 NA reconstruction and serve
the MH1 EU (`SLES_527.07`) build, which shares the lobby and hunt protocol, on
the same server for crossplay.
"""

from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
import logging
import os
from pathlib import Path
import socket
import struct

from opensnap.config import AppConfig
from opensnap.core.bootstrap.handlers import _resolve_advertise_host
from opensnap_app.capcom.connection import AppConnection
from opensnap_app.capcom.protocol import (
    APP_DIRECTION_PUSH,
    APP_DIRECTION_REPLY,
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
from opensnap_app.capcom.monsterhunter.content import EventCatalog, InformationPages, MarketState, NamedFiles
from opensnap_app.capcom.monsterhunter.flows import AppFlowTracker, AppPhase
from opensnap_app.capcom.monsterhunter.patch import ClientPatch, load_patches
from opensnap_app.capcom.monsterhunter.records import (
    HRP_BOARD,
    HUNTER_SUMMARY,
    HUNTER_SUMMARY_SIZE,
    RecordPages,
    decode_hunter_summary,
    decode_quest_record,
    decode_test_build_quest_record,
    load_quests,
    store_quest_record,
    store_test_build_quest_record,
)
from opensnap.core.online import OnlinePlayer
from opensnap.plugins.monsterhunter.directory import Directory
from opensnap.plugins.monsterhunter.profile import MonsterHunterProfile
from opensnap.storage.interfaces import OnlinePlayerStore, RecordStore, StandingStore

_LOGGER = logging.getLogger('opensnap_app.capcom.monsterhunter')

DEFAULT_DATA_DIRECTORY = 'data'
# NA connection kind: after its 1002 reply only mode 0 (APP1) waits silently
# for the server (state 49); modes 1-5 send their first request right away
# (1007, 6203, 6101, 6502, 6501; `0x002aeb40`, mode table `0x003959f0`).
NA_FIRST_REQUEST_WAIT_SECONDS = 0.5
PHASE_CLOSE_LINGER_SECONDS = 2.0
CONNECTION_TIMEOUT_SECONDS = 20.0

# Client APP commands (`SLUS_208.96` request table `0x003698f0`/`0x00369940`,
# reply handlers `0x00369a30`; `SLES_527.07` table `0x0032aa50`).
CMD_APP_WORLD = 0x1007
CMD_APP_PATCH_CHUNK = 0x6002
CMD_APP_FILE_INFO = 0x6101
CMD_APP_FILE_CHUNK = 0x6102
CMD_APP_WORLD_6103 = 0x6103
# EU only: 6110 network settings, 6210 hunt engine tuning, 6220/6221 login
# auth suffix, 6307 quest records.
CMD_APP_NETWORK_SETTINGS = 0x6110
CMD_APP_HUNT_TUNING = 0x6210
CMD_APP_AUTH_SUFFIX = 0x6220
CMD_APP_QUEST_RECORD = 0x6307
# Download lists; EU APP1 asks for them after the TOP_INFOR page. Each is a
# `u16 count, count x u32 size` reply the client pulls page-by-page; count 0
# means "nothing", which keeps the client's built-in pages. They are three
# distinct lists (different buffers, consumers and page caps), not one:
# - 6401: up to 8 on-screen text pages (<= 1 KB each, `<BR>/<SIZE>/<END>`
#   markup) shown in the paged notice viewer during the DNAS/online-connect
#   flow -- the same role as Resident Evil Outbreak's DNAS/SCEA notice pages
#   (handler NA `0x002aff70`, viewer `0x00245380`; clamps count to 8).
# - 6403: up to 2 pages (handler NA `0x002b0490`).
# - 6105: up to 8 pages (handler NA `0x002b1380`).
# openSNAP answers all three with count 0. (The NA client does not clamp the
# count it reads for 0x6203/0x6503, so never over-count those in a reply.)
CMD_APP_DOWNLOAD_LIST = 0x6401
CMD_APP_DOWNLOAD_LIST_6403 = 0x6403
CMD_APP_DOWNLOAD_LIST_6105 = 0x6105
CMD_APP_INFORMATION_LIST = 0x6201
CMD_APP_INFORMATION_DOWNLOAD = 0x6202
CMD_APP_EVENT_CATALOG = 0x6203
CMD_APP_EVENT_DOWNLOAD = 0x6204
CMD_APP_MARKET = 0x6211
CMD_APP_EVENT_AVAILABILITY = 0x6212
CMD_APP_CONNECTION_TIMING = 0x6213
CMD_APP_RETURN = 0x6306
# 6320/6321: the resource upload that follows every online quest return
# (NA 6306 reply `0x002b3ba0` announces type 1; EU after its 6307 records) and
# EU APP1 (type 2, after 6105). The client announces `u8 type, u32 size`
# (sizes NA `0x0055f6c0`: type 1 -> 20, type 2 -> 304; NA `0x002b3780`, EU
# `0x002a3930`) and the reply's `u8` decides (NA `0x002b3840`, EU
# `0x002a39f0`):
# - 0, or any type without a size: nothing is uploaded; the client completes
#   with 1004.
# - the announced type: the client uploads it as 6321 `u8 type, u32 0,
#   u16 size, field(resource)` (NA `0x002b3920`, EU `0x002a3ad0`).
# - the other type: the client announces that type with 6320 again.
# Type 1 is the hunter summary with the Hunter Rank Points (`records.py`);
# type 2 is the equipment box (100 x u8 type, then 100 x u16 id, then the u32
# rare-item mask; NA `0x0055f720`). The 6321 reply handlers check only the
# error byte, then complete with 1004 (NA `0x002b3a60`, EU `0x002a3c10`).
CMD_APP_RETURN_RESOURCE = 0x6320
CMD_APP_RESOURCE_UPLOAD = 0x6321
RESOURCE_NONE = 0
CMD_APP_SERVER_ADDRESS = 0x6501
# 6502 asks for one World's status (`u32 token, field World key`); a reply
# flag of 1 loads that World's Lands, 0 skips the World (`0x002b2270`).
CMD_APP_WORLD_STATUS = 0x6502
CMD_APP_WORLD_LIST = 0x6503
CMD_APP_LAND_DESCRIPTOR = 0x6504
CMD_APP_LAND_POPULATION = 0x6510

# The APP endpoint opens every connection with server 1002; the client answers
# 1002 with `field, u8 build, u8 flags, field, 4 x u8` (NA `0x002aeb40`). NA
# sends build 2, flags 1 (`0x00247fc8`); EU sends build 3, flags
# `language | 0x10`. NA starts its connection mode in that handler (APP1 then
# waits for 6001, `0x003959f0`); EU waits for a server 6001 and starts its mode
# there (`0x002a4630`). EU APP1 is mode 6, whose first request is 6110.
APP_BUILD_NA = 2
APP_BUILD_EU = 3
APP_LANGUAGE_MASK = 0x0F
# The EU test build `TLES_527.07` answers like EU (build 3, `0x00247910`) but
# runs the NA APP client, so it is served the NA flow. Its version field
# (`0x00397ce0`) is its own build stamp, `YYMMDDhhmm` of `__DATE__`/`__TIME__`
# (`0x0010fce0`): Aug 13 2004 10:08:56. Only an applied client patch would
# replace it (`0x002576a8`), and openSNAP offers none.
TLES_VERSION = b'0408131008'
# The NA public beta `SLUS_291.10` answers like NA (build 2, flags 1,
# `0x00246518`) with its own stamp (`0x00394fe0`, `0x0010fcb0`): May 17 2004
# 19:56:19. It is served by its own `AppService` (its own players, Worlds and
# data), chosen by `CapcomAppServer` from this stamp.
NA_PUBLIC_BETA_VERSION = b'0405171956'
# 6001 is a server push (direction 0x10: both builds dispatch it only with
# that direction, NA `0x002b4b48`, EU `0x002a5060`). Only APP1 reads its
# `u8 flag` (NA `0x002b4150`, EU `0x002a46f8`): 0 goes on to the Information
# page; 1 (`field version, u32 size, u32`) first downloads a client patch over
# 6002, which the client saves only when one was received (NA `0x00248170`).
# A client whose 1002 version a built patch is offered to (its stock stamp)
# gets that patch; once installed its version is the patch name, so it gets
# `VERSION_NO_PATCH` like everyone else (`patch.py`).
# 6110 `u16 port parameter, field regweb URL, field bootstrap host`; 0 and
# empty fields keep the client's defaults (`0x002a1580`).
EU_NETWORK_SETTINGS_DEFAULTS = struct.pack('>H', 0)
# 6220 `u8 1, u32 size`; size 0 keeps the built-in auth suffix and skips the
# 6221 download (`0x002a17b0`).
EU_AUTH_SUFFIX_DEFAULT = b'\x01' + bytes(4)
# 6210 tunes the hunt network engine (`mcsls_*`, names from the JP symbols).
# The reply is 17 words; the first three are IEEE floats in seconds, applied
# only when nonzero, and the other 14 are read and dropped (EU `0x0029f320`;
# NA receives the same three as a server push, `0x002af700`):
# - word 0, default 30.0: hunt peer timeout. `mcsls_recv` drops a peer slot
#   that has sent nothing for longer than this (NA `0x001fb668`).
# - word 1, default 0.25: send interval of the throttled outbound queue. Each
#   tick adds the frame time to an accumulator (seeded with the interval by
#   `mcsls_init`, so the first send is immediate) and the queue is flushed
#   once it reaches the interval (NA `0x001face0`).
# - word 2, default 0.0: the same interval for the second outbound queue; 0
#   flushes it on every tick (NA `0x001fade0`).
# Zeros keep these defaults. NA and EU ship the same values (EU data
# `0x0034c394`/`0x0034c39c`, third word zero-initialised), so mixed hunts
# time out and pace their traffic alike.
EU_HUNT_TUNING_DEFAULTS = bytes(17 * 4)

# 6213 reply: eight big-endian frame counts (`0x002aef00`). The client's own
# defaults (used when the reply carries error `0xFF`) are sent unchanged; the
# last two are the lobby echo keepalive interval and reply timeout read by the
# lobby tick (`0x0061d440`). The NA public beta reads the first four
# (`0x002ad090`, the same getters as the release's first four).
CONNECTION_TIMING_FRAMES = (20, 300, 300, 5400, 1800, 1800, 1800, 1800)


@dataclass(frozen=True, slots=True)
class AppServiceConfig:
    """Data folder and game server address of the APP service."""

    data_directory: Path
    profile: MonsterHunterProfile
    # Host of the Monster Hunter game server; `0.0.0.0` means this machine,
    # advertised by its address towards each client.
    game_host: str


def read_app_service_config(config: AppConfig, profile: MonsterHunterProfile) -> AppServiceConfig:
    """Build APP settings; the game server is found like the bootstrap finds it (`OPENSNAP_GAME_SERVER_MAP`)."""

    data_root = os.getenv('OPENSNAP_DATA_DIR', '').strip() or DEFAULT_DATA_DIRECTORY
    target = config.server.resolve_game_target(profile.identifier)
    return AppServiceConfig(
        data_directory=Path(data_root) / profile.data_directory,
        profile=profile,
        game_host=target.host if target is not None else config.server.game.host,
    )


class AppService:
    """Serve every Monster Hunter APP connection, NA and EU."""

    def __init__(
        self,
        *,
        config: AppServiceConfig,
        flows: AppFlowTracker,
        directory: Directory,
        online_players: OnlinePlayerStore,
        records: RecordStore,
        standings: StandingStore,
    ) -> None:
        self._config = config
        self._flows = flows
        self._directory = directory
        self._online_players = online_players
        self._records = records
        self._standings = standings
        self._market = MarketState()
        self._events = EventCatalog(config.data_directory)
        self._information = InformationPages(config.data_directory)
        self._record_pages = RecordPages(
            records, standings, config.profile.identifier, load_quests(config.data_directory)
        )
        self._files = NamedFiles(config.data_directory, self._record_pages.render)
        self._patches = load_patches(config.data_directory)

    def serve(self, connection: AppConnection, answer: ConnectAnswer) -> None:
        host = connection.host
        self._flows.sync(self._players())
        owner, phase = self._flows.claim(host)
        test_build = answer.build == APP_BUILD_EU and answer.version[:len(TLES_VERSION)] == TLES_VERSION
        client = _MhClient(
            connection, phase, owner, APP_BUILD_NA if test_build else answer.build, test_build,
            login=answer.login.split(b'\x00', 1)[0].decode('ascii', errors='replace'),
        )
        client.patch = self._patches.get(answer.version.split(b'\x00', 1)[0])
        language = answer.flags & APP_LANGUAGE_MASK if client.build == APP_BUILD_EU else 0
        _LOGGER.info(
            'APP connection from %s:%d build %s%s phase=%s owner=%s.',
            host,
            connection.port,
            'TLES (NA flow)' if test_build else 'NA' if client.build == APP_BUILD_NA else 'EU',
            f' language {language}' if language else '',
            phase,
            'none' if owner is None else f'0x{owner:08x}',
        )
        try:
            connection.set_timeout(CONNECTION_TIMEOUT_SECONDS)
            if client.build == APP_BUILD_NA and connection.poll(NA_FIRST_REQUEST_WAIT_SECONDS):
                # NA modes 1-5 open with their own request; never APP1.
                if client.phase is AppPhase.APP1:
                    client.phase = self._online_phase(connection.peek_command())
            else:
                push = self._version_push(client.patch)
                connection.send(app_frame(APP_DIRECTION_PUSH, CMD_APP_VERSION, payload=push))
                if client.build == APP_BUILD_NA:
                    # Only NA APP1 (mode 0) waits for 6001.
                    client.phase = AppPhase.APP1
                # EU names its mode with its first request (APP1 opens with 6110,
                # or with 6002 when it downloads the offered patch).
                elif client.phase is AppPhase.APP1 and connection.poll(CONNECTION_TIMEOUT_SECONDS):
                    if connection.peek_command() not in (CMD_APP_NETWORK_SETTINGS, CMD_APP_PATCH_CHUNK):
                        client.phase = self._online_phase(connection.peek_command())
            self._serve_online(client)
        except (ConnectionError, socket.timeout) as exc:
            _LOGGER.info('APP connection from %s:%d closed (%s): %s', host, connection.port, client.phase, exc)
        except Exception:  # noqa: BLE001
            _LOGGER.exception('APP connection from %s:%d failed (%s).', host, connection.port, client.phase)

    @staticmethod
    def _version_push(patch: ClientPatch | None) -> bytes:
        """6001: `u8 0`, or `u8 1, field (name + version), u32 size, u32 0` offering `patch`."""

        if patch is None:
            return VERSION_NO_PATCH
        return b'\x01' + encode_app_field(patch.name + patch.version, 0) + struct.pack('>2L', len(patch.blob), 0)

    def _players(self) -> list[OnlinePlayer]:
        return self._online_players.list(self._config.profile.identifier)

    @staticmethod
    def _online_phase(command: int) -> AppPhase:
        """Connection kind of a client that is not in APP1, from its first request.

        The client's own mode decides it, whatever the flow tracker last saw (a
        logout between KICS and World selection resets the host to APP1).
        """

        return AppPhase.WORLD if command == CMD_APP_WORLD else AppPhase.LAND

    def _serve_online(self, client: '_MhClient') -> None:
        """Answer one client-driven connection of the APP state machine.

        WORLD connections start with 1007 and LAND connections with 6501 or
        6502 (`0x002aeb40` mode table). A WORLD connection that publishes
        Information pages continues through Event, Market and connection
        timing into Land information (`0x002af0c0` -> 6213 -> 6501); EU skips
        Information on its own (6212 -> 6203, `0x0029eef0`). EU APP1 asks for
        its settings and TOP_INFOR page (6110 -> 6220 -> 6210 -> 6101 -> 6401
        -> 6403 -> 6105 -> 6320 [-> 6320 -> 6321] -> 1004); NA APP1 asks for its TOP_INFOR page
        and server address (6101 -> 6401 -> 6501 -> 1004).
        """

        host, owner = client.app.host, client.owner
        advertise_host = _resolve_advertise_host(
            configured_host=self._config.game_host,
            bind_host=self._config.game_host,
            client_host=host,
        )
        while True:
            request = client.app.receive()
            command = frame_command(request)
            if command == CMD_APP_WORLD:
                client.phase = AppPhase.WORLD
                self._flows.advance(host, owner, AppPhase.WORLD)
            elif command in (CMD_APP_SERVER_ADDRESS, CMD_APP_WORLD_STATUS) and client.phase is AppPhase.WORLD:
                client.phase = AppPhase.LAND
                self._flows.advance(host, owner, AppPhase.LAND)
            elif command == CMD_APP_COMPLETE:
                client.app.reply(request)
                if client.phase is AppPhase.APP1:
                    pass
                elif client.phase is AppPhase.WORLD:
                    self._flows.advance(host, owner, AppPhase.POSTWORLD)
                elif client.phase is AppPhase.POSTWORLD:
                    self._flows.advance(host, owner, AppPhase.LAND)
                else:
                    self._flows.release(host, owner)
                client.app.linger(PHASE_CLOSE_LINGER_SECONDS)
                return

            sequence = frame_sequence(request)
            try:
                reply = self._reply(request, client, advertise_host)
            except ValueError as exc:
                _LOGGER.warning('Rejecting APP command 0x%04x: %s', command, exc)
                client.app.send(app_frame(APP_DIRECTION_REPLY, command, sequence=sequence, error=0xFF))
                continue
            if reply is None:
                _LOGGER.warning('Unexpected APP %s command 0x%04x; not answered.', client.phase, command)
                continue
            client.app.reply(request, reply)

    def _reply(self, request: bytes, client: '_MhClient', advertise_host: str) -> bytes | None:
        command = frame_command(request)
        sequence = frame_sequence(request)
        payload = frame_payload(request)
        phase = client.phase

        def encode_field(data: bytes) -> bytes:
            return encode_app_field(data, sequence)

        def decode_field(data: bytes) -> tuple[bytes, int]:
            return decode_app_field(data, sequence)

        if command == CMD_APP_WORLD:
            return b'\x00'
        if command == CMD_APP_PATCH_CHUNK:
            return self._patch_chunk(payload, client.patch, decode_field, encode_field)
        if command == CMD_APP_WORLD_6103:
            return b'\x00\x00'
        if command == CMD_APP_EVENT_AVAILABILITY:
            return self._events.availability()
        if command == CMD_APP_INFORMATION_LIST:
            return self._information.listing(encode_field)
        if command == CMD_APP_INFORMATION_DOWNLOAD:
            return self._information.download(payload, encode_field)
        if command == CMD_APP_EVENT_CATALOG:
            return self._events.catalog(encode_field)
        if command == CMD_APP_EVENT_DOWNLOAD:
            return self._events.download(payload, encode_field)
        if command == CMD_APP_MARKET:
            return self._market.native_response()
        if command == CMD_APP_CONNECTION_TIMING:
            words = CONNECTION_TIMING_FRAMES[:self._config.profile.connection_timing_words]
            return struct.pack(f'>{len(words)}H', *words)
        if command == CMD_APP_SERVER_ADDRESS:
            # `u8 flag`: 1 + `u32 address, u32` loads the Worlds; NA APP1 takes
            # 0 and completes with 1004 (`0x002b19f0`).
            if phase is AppPhase.APP1:
                return b'\x00'
            return b'\x01' + socket.inet_aton(advertise_host) + bytes(4)
        if command == CMD_APP_WORLD_STATUS:
            return b'\x01\x00\x00\x00\x01\x00\x00\x00\x00'
        if command == CMD_APP_WORLD_LIST:
            return self._world_list(payload, advertise_host, encode_field)
        if command == CMD_APP_LAND_DESCRIPTOR:
            return self._land_list(payload, phase, sequence, advertise_host, encode_field)
        if command == CMD_APP_LAND_POPULATION:
            return self._land_populations(payload, sequence, advertise_host)
        if command == CMD_APP_RETURN:
            return b''
        if command == CMD_APP_RETURN_RESOURCE:
            asks = self._config.profile.uploads_hunter_summary and not client.test_build
            return bytes((HUNTER_SUMMARY if asks else RESOURCE_NONE,))
        if command == CMD_APP_RESOURCE_UPLOAD:
            self._store_hunter_summary(payload, client, decode_field)
            return b''
        if command == CMD_APP_FILE_INFO:
            return self._files.info(payload, decode_field, encode_field)
        if command == CMD_APP_FILE_CHUNK:
            return self._files.download(payload, decode_field, encode_field)
        if command in (CMD_APP_DOWNLOAD_LIST, CMD_APP_DOWNLOAD_LIST_6403, CMD_APP_DOWNLOAD_LIST_6105):
            # `u16 count`; nothing to download.
            return b'\x00\x00'
        if command == CMD_APP_NETWORK_SETTINGS:
            return EU_NETWORK_SETTINGS_DEFAULTS + encode_field(b'') + encode_field(b'')
        if command == CMD_APP_AUTH_SUFFIX:
            return EU_AUTH_SUFFIX_DEFAULT
        if command == CMD_APP_HUNT_TUNING:
            return EU_HUNT_TUNING_DEFAULTS
        if command == CMD_APP_QUEST_RECORD:
            # Both reply handlers only check the error byte (EU, TLES `0x002b2f20`).
            self._store_quest_record(payload, client)
            return b''
        return None

    @staticmethod
    def _patch_chunk(
        request: bytes,
        patch: ClientPatch | None,
        decode_field: Callable[[bytes], tuple[bytes, int]],
        encode_field: Callable[[bytes], bytes],
    ) -> bytes:
        """Answer 6002 `field name, u32 offset, u16 length` with `field name, u32 offset, field data`.

        The client checks the name (10 characters) and stores the data at the
        offset (EU `0x002a4980`).
        """

        name, size = decode_field(request)
        if len(request) != size + 6:
            raise ValueError('patch chunk request has an invalid size')
        offset, length = struct.unpack_from('>LH', request, size)
        if patch is None or name != patch.name or offset >= len(patch.blob):
            raise ValueError('patch chunk request does not match the offered patch')
        return encode_field(name) + struct.pack('>L', offset) + encode_field(patch.blob[offset:offset + length])

    def _store_quest_record(self, payload: bytes, client: '_MhClient') -> None:
        """Keep one 6307 quest record for the connection's player.

        The acknowledgement does not depend on it: the client only waits for
        the empty reply before it adds the counts to its save.
        """

        test_build = client.test_build
        record = decode_test_build_quest_record(payload) if test_build else decode_quest_record(payload)
        player = self._player(client)
        if player is None:
            _LOGGER.warning(
                'Quest record from %s login %r has no logged-in player; not stored: %s',
                client.app.host,
                client.login,
                record,
            )
            return
        store = store_test_build_quest_record if test_build else store_quest_record
        store(self._records, self._config.profile.identifier, player, record)
        _LOGGER.info('Stored quest record for %s: %s', player.username, record)

    def _store_hunter_summary(
        self, payload: bytes, client: '_MhClient', decode_field: Callable[[bytes], tuple[bytes, int]]
    ) -> None:
        """Keep the Hunter Rank Points of a 6321 hunter summary as the player's `hrp` standing.

        It is acknowledged whatever it holds: the ranking is optional and the
        client's quest return must go on.
        """

        try:
            if len(payload) < 7:
                raise ValueError('truncated')
            kind, size = struct.unpack_from('>B4xH', payload)
            block, length = decode_field(payload[7:])
            if kind != HUNTER_SUMMARY or size != HUNTER_SUMMARY_SIZE or len(payload) != 7 + length:
                raise ValueError('not a hunter summary')
            points = decode_hunter_summary(block)
        except ValueError as exc:
            _LOGGER.warning('Ignoring resource upload from %s: %s', client.app.host, exc)
            return
        player = self._player(client)
        if player is None:
            # EU APP1 uploads before the KICS login.
            _LOGGER.info('Hunter summary from %s login %r has no logged-in player; HRP %d not stored.',
                         client.app.host, client.login, points)
            return
        self._standings.set(
            game=self._config.profile.identifier,
            board=HRP_BOARD,
            user_id=player.user_id,
            player=player.username,
            score=points,
        )
        _LOGGER.info('Stored HRP %d for %s.', points, player.username)

    def _player(self, client: '_MhClient') -> OnlinePlayer | None:
        """The connection's logged-in player.

        The 1002 answer's login is the client ID it logged in with (MH NA
        `0x002aeb40`). The address alone cannot tell apart players who share
        one, e.g. two consoles in one home.
        """

        return next(
            (
                player for player in self._players()
                if player.host == client.app.host and player.username == client.login
            ),
            None,
        )

    def _world_list(self, request: bytes, advertise_host: str, encode_field: Callable[[bytes], bytes]) -> bytes:
        """Answer 6503 `u16 start, u16 count` with one page of Worlds.

        Reply (`0x002b1c60`): `u16 total, u16 start, u8 count`, then per World
        key field (62), name field (15), 8 bytes, description field (255). The
        key is the World's game server host; the World menu shows the name and
        description (`0x00613320`).
        """

        if len(request) < 4:
            raise ValueError('World list request is truncated.')
        start, count = struct.unpack_from('>2H', request)
        worlds = self._directory.worlds[start:start + count]
        entries = b''.join(
            encode_field((world.host or advertise_host).encode('ascii'))
            + encode_field(world.name.encode('ascii'))
            + bytes(8)
            + encode_field(world.description.encode('ascii'))
            for world in worlds
        )
        return struct.pack('>2HB', len(self._directory.worlds), start, len(worlds)) + entries

    def _land_list(
        self,
        request: bytes,
        phase: AppPhase,
        sequence: int,
        advertise_host: str,
        encode_field: Callable[[bytes], bytes],
    ) -> bytes:
        """Answer 6504 `u16 start, u16 count, field World key` with one page of that World's Lands.

        Reply (`0x002b2840`): `u16 total, u16 start, u8 count`, then per Land
        key field (15), name field (15), 8 bytes, description field (255),
        `u16 area count, u16 capacity, u32 color`. POSTWORLD publishes the
        entries without these words, as the runtime-validated server did.

        `lobby.bin` uses the Area count for the Area menu rows (`0x00601a08`)
        and for summing Area populations after the Area query (`0x006129f8`);
        a sum at or above the capacity reports the Land as full (`0x00612b14`).
        The color tints the Land menu panel (`0x006022d8`).
        """

        if len(request) < 4:
            raise ValueError('Land list request is truncated.')
        start, count = struct.unpack_from('>2H', request)
        key, _ = decode_app_field(request[4:], sequence)
        world = self._directory.world_by_host(key.rstrip(b'\x00').decode('ascii', errors='ignore'), advertise_host)
        if world is None:
            raise ValueError('Land list request names an unknown World.')
        lands = world.lands[start:start + count]
        entries = b''.join(
            encode_field(land.key.encode('ascii'))
            + encode_field(land.name.encode('ascii'))
            + bytes(8)
            + encode_field(land.description.encode('ascii'))
            + (
                bytes(8)
                if phase is AppPhase.POSTWORLD
                else struct.pack(
                    '>2HL', len(land.areas), land.capacity, land.color
                )
            )
            for land in lands
        )
        return struct.pack('>2HB', len(world.lands), start, len(lands)) + entries

    def _land_populations(self, request: bytes, sequence: int, advertise_host: str) -> bytes:
        """Answer 6510 `u8 count, count x field key, field World key` (`0x002b3220`).

        Reply (`0x002b3410`): `u8 count, count x u16 population, field world`.
        Lands of a World hosted elsewhere report 0; this server does not see
        their players.
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
        world_key, _ = decode_app_field(world_field, sequence)
        world = self._directory.world_by_host(world_key.rstrip(b'\x00').decode('ascii', errors='ignore'), advertise_host)
        players_by_area = Counter(player.area_id for player in self._players())
        populations = []
        for key in keys:
            land = None if world is not self._directory.local_world else world.land_by_key(key)
            populations.append(0 if land is None else sum(players_by_area[area.area_id] for area in land.areas))
        return struct.pack(f'>B{len(populations)}H', len(populations), *populations) + world_field


@dataclass(slots=True)
class _MhClient:
    """One APP connection with its Monster Hunter phase, flow owner and client build."""

    app: AppConnection
    phase: AppPhase
    owner: int | None
    # APP client the connection runs (`APP_BUILD_NA` / `APP_BUILD_EU`).
    build: int
    # `TLES_527.07`: the NA client plus its own 6307 (`0x002b2da0`).
    test_build: bool = False
    # Login ID from the client's 1002 answer.
    login: str = ''
    # The client patch offered on this connection.
    patch: ClientPatch | None = None
