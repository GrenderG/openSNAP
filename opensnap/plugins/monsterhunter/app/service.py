"""Monster Hunter APP TCP service.

The client opens short TCP connections to the APP endpoint around its SNAP
session: APP1 before bootstrap login, then WORLD, POSTWORLD, and LAND after
KICS login, plus LAND reconnects after quests. The exchanges below reproduce
the runtime-validated server responses of the MH1 NA reconstruction and serve
the MH1 EU (`SLES_527.07`) build, which shares the lobby and hunt protocol, on
the same server for crossplay.
"""

from collections.abc import Callable
from dataclasses import dataclass
import logging
import os
from pathlib import Path
import socket
import struct
import threading
import time

from opensnap.config import AppConfig
from opensnap.core.bootstrap.handlers import _resolve_advertise_host
from opensnap.plugins.monsterhunter.app.codec import (
    APP_DIRECTION_PUSH,
    APP_DIRECTION_REPLY,
    APP_DIRECTION_SERVER,
    app_frame,
    decode_app_field,
    encode_app_field,
    frame_command,
    frame_payload,
    frame_sequence,
    pop_app_frame,
)
from opensnap.core.sessions import Session
from opensnap.plugins.monsterhunter.app.content import EventCatalog, InformationPages, MarketState, NamedFiles
from opensnap.plugins.monsterhunter.app.flows import AppFlowTracker, AppPhase
from opensnap.plugins.monsterhunter.app.records import (
    RecordPages,
    decode_quest_record,
    load_quests,
    store_quest_record,
)
from opensnap.plugins.monsterhunter.directory import Directory, Land
from opensnap.plugins.monsterhunter.profile import MonsterHunterProfile
from opensnap.storage.interfaces import RecordStore

_LOGGER = logging.getLogger('opensnap.plugins.monsterhunter.app')

DEFAULT_APP_PORT = 10127
DEFAULT_DATA_DIRECTORY = 'data'
# NA connection kind: after its 1002 reply only mode 0 (APP1) waits silently
# for the server (state 49); modes 1-5 send their first request right away
# (1007, 6203, 6101, 6502, 6501; `0x002aeb40`, mode table `0x003959f0`).
NA_FIRST_REQUEST_WAIT_SECONDS = 0.5
PHASE_CLOSE_LINGER_SECONDS = 2.0
CONNECTION_TIMEOUT_SECONDS = 20.0

# Client APP commands (`SLUS_208.96` request table `0x003698f0`/`0x00369940`,
# reply handlers `0x00369a30`; `SLES_527.07` table `0x0032aa50`).
CMD_APP_CONNECT = 0x1002
CMD_APP_COMPLETE = 0x1004
CMD_APP_WORLD = 0x1007
CMD_APP_VERSION = 0x6001
CMD_APP_FILE_INFO = 0x6101
CMD_APP_FILE_CHUNK = 0x6102
CMD_APP_WORLD_6103 = 0x6103
# EU only: 6110 network settings, 6210 hunt engine tuning, 6220/6221 login
# auth suffix, 6307 quest records.
CMD_APP_NETWORK_SETTINGS = 0x6110
CMD_APP_HUNT_TUNING = 0x6210
CMD_APP_AUTH_SUFFIX = 0x6220
CMD_APP_QUEST_RECORD = 0x6307
# Download lists; EU APP1 asks for them after the TOP_INFOR page.
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
CMD_APP_RETURN_RESOURCE = 0x6320
CMD_APP_SERVER_ADDRESS = 0x6501
# 6502 asks for one World's status (`u32 token, field World key`); a reply
# flag of 1 loads that World's Lands, 0 skips the World (`0x002b2270`).
CMD_APP_WORLD_STATUS = 0x6502
CMD_APP_WORLD_LIST = 0x6503
CMD_APP_LAND_DESCRIPTOR = 0x6504
CMD_APP_LAND_POPULATION = 0x6510

# Every APP connection opens with server 1002; the client answers 1002 with
# `field, u8 build, u8 flags, field, 4 x u8` (NA `0x002aeb40`). NA sends build
# 2, flags 1 (`0x00247fc8`); EU sends build 3, flags `language | 0x10`. NA
# starts its connection mode in that handler (APP1 then waits for 6001,
# `0x003959f0`); EU waits for a server 6001 and starts its mode there
# (`0x002a4630`). EU APP1 is mode 6, whose first request is 6110.
APP_BUILD_NA = 2
APP_BUILD_EU = 3
APP_LANGUAGE_MASK = 0x0F
# 6001 is a server push (direction 0x10: both builds dispatch it only with
# that direction, NA `0x002b4b48`, EU `0x002a5060`). Only APP1 reads its
# `u8 flag` (NA `0x002b4150`, EU `0x002a46f8`): 0 goes on to the Information
# page; 1 (`field version, u32 size, u32`) first downloads a client patch over
# 6002, which the client saves only when one was received (NA `0x00248170`).
# openSNAP ships no client patch.
VERSION_NO_PATCH = b'\x00'
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
# lobby tick (`0x0061d440`).
CONNECTION_TIMING_FRAMES = (20, 300, 300, 5400, 1800, 1800, 1800, 1800)


@dataclass(frozen=True, slots=True)
class AppServiceConfig:
    """Listener and advertised-address settings for the APP service."""

    host: str
    port: int
    data_directory: Path
    profile: MonsterHunterProfile
    advertise_host: str
    game_bind_host: str


def read_app_service_config(config: AppConfig, profile: MonsterHunterProfile) -> AppServiceConfig:
    """Build APP settings from the game server config and environment."""

    host = os.getenv('OPENSNAP_MH_APP_HOST', '').strip() or config.server.game.host
    port = int(os.getenv('OPENSNAP_MH_APP_PORT', '').strip() or DEFAULT_APP_PORT)
    data_root = os.getenv('OPENSNAP_DATA_DIR', '').strip() or DEFAULT_DATA_DIRECTORY
    return AppServiceConfig(
        host=host,
        port=port,
        data_directory=Path(data_root) / profile.data_directory,
        profile=profile,
        advertise_host=config.server.game.advertise_host,
        game_bind_host=config.server.game.host,
    )


class AppService:
    """Threaded TCP listener; one worker thread per client connection."""

    def __init__(
        self,
        *,
        config: AppServiceConfig,
        flows: AppFlowTracker,
        directory: Directory,
        land_population: Callable[[Land], int],
        records: RecordStore,
        session: Callable[[int], Session | None],
    ) -> None:
        self._config = config
        self._flows = flows
        self._directory = directory
        self._land_population = land_population
        self._records = records
        self._session = session
        self._market = MarketState()
        self._events = EventCatalog(config.data_directory)
        self._information = InformationPages(config.data_directory)
        self._record_pages = RecordPages(records, config.profile.identifier, load_quests(config.data_directory))
        self._files = NamedFiles(config.data_directory, self._record_pages.render)
        self._listener: socket.socket | None = None
        self._stopped = threading.Event()

    def start(self) -> None:
        """Bind the listener now so bind errors surface at startup, then accept in background."""

        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            listener.bind((self._config.host, self._config.port))
            listener.listen(16)
        except OSError:
            listener.close()
            raise
        listener.settimeout(0.25)
        self._listener = listener
        threading.Thread(target=self._accept_loop, name='mh-app', daemon=True).start()
        _LOGGER.info('APP service listening on %s:%d.', self._config.host, self._config.port)

    def stop(self) -> None:
        self._stopped.set()

    def _accept_loop(self) -> None:
        assert self._listener is not None
        with self._listener:
            while not self._stopped.is_set():
                try:
                    client, address = self._listener.accept()
                except socket.timeout:
                    continue
                except OSError:
                    _LOGGER.exception('APP accept failed; continuing.')
                    continue
                threading.Thread(target=self._serve, args=(client, address), daemon=True).start()

    def _serve(self, client: socket.socket, address: tuple[str, int]) -> None:
        host = address[0]
        owner, phase = self._flows.claim(host)
        _LOGGER.info(
            'APP connection from %s:%d phase=%s owner=%s.',
            host,
            address[1],
            phase,
            'none' if owner is None else f'0x{owner:08x}',
        )
        connection = _AppConnection(client, phase, host, owner)
        try:
            client.settimeout(CONNECTION_TIMEOUT_SECONDS)
            self._handshake(connection)
            if connection.build == APP_BUILD_NA and connection.poll(NA_FIRST_REQUEST_WAIT_SECONDS):
                # NA modes 1-5 open with their own request; never APP1.
                if connection.phase is AppPhase.APP1:
                    connection.phase = self._online_phase(connection.peek_command())
            else:
                connection.send(app_frame(APP_DIRECTION_PUSH, CMD_APP_VERSION, payload=VERSION_NO_PATCH))
                if connection.build == APP_BUILD_NA:
                    # Only NA APP1 (mode 0) waits for 6001.
                    connection.phase = AppPhase.APP1
                # EU names its mode with its first request (APP1 opens with 6110).
                elif connection.phase is AppPhase.APP1 and connection.poll(CONNECTION_TIMEOUT_SECONDS):
                    if connection.peek_command() != CMD_APP_NETWORK_SETTINGS:
                        connection.phase = self._online_phase(connection.peek_command())
            self._serve_online(connection)
        except (ConnectionError, socket.timeout) as exc:
            _LOGGER.info('APP connection from %s:%d closed (%s): %s', host, address[1], connection.phase, exc)
        except Exception:  # noqa: BLE001
            _LOGGER.exception('APP connection from %s:%d failed (%s).', host, address[1], connection.phase)
        finally:
            client.close()

    def _handshake(self, connection: '_AppConnection') -> None:
        """Open with server 1002 and read the client's build from its 1002 reply."""

        connection.send(app_frame(APP_DIRECTION_SERVER, CMD_APP_CONNECT, payload=b'\x00\x00'))
        reply = connection.receive()
        if frame_command(reply) != CMD_APP_CONNECT:
            raise ConnectionError(f'APP expected 0x{CMD_APP_CONNECT:04x}, got 0x{frame_command(reply):04x}.')
        payload = frame_payload(reply)
        _, size = decode_app_field(payload, frame_sequence(reply))
        if len(payload) < size + 2 or payload[size] not in (APP_BUILD_NA, APP_BUILD_EU):
            raise ConnectionError('APP connect reply carries an unknown client build.')
        connection.build = payload[size]
        language = payload[size + 1] & APP_LANGUAGE_MASK if connection.build == APP_BUILD_EU else 0
        _LOGGER.info(
            'APP client build %s%s.',
            'NA' if connection.build == APP_BUILD_NA else 'EU',
            f' language {language}' if language else '',
        )

    @staticmethod
    def _online_phase(command: int) -> AppPhase:
        """Connection kind of a client that is not in APP1, from its first request.

        The client's own mode decides it, whatever the flow tracker last saw (a
        logout between KICS and World selection resets the host to APP1).
        """

        return AppPhase.WORLD if command == CMD_APP_WORLD else AppPhase.LAND

    def _serve_online(self, connection: '_AppConnection') -> None:
        """Answer one client-driven connection of the APP state machine.

        WORLD connections start with 1007 and LAND connections with 6501 or
        6502 (`0x002aeb40` mode table). A WORLD connection that publishes
        Information pages continues through Event, Market and connection
        timing into Land information (`0x002af0c0` -> 6213 -> 6501); EU skips
        Information on its own (6212 -> 6203, `0x0029eef0`). EU APP1 asks for
        its settings and TOP_INFOR page (6110 -> 6220 -> 6210 -> 6101 -> 6401
        -> 6403 -> 6105 -> 6320 -> 1004); NA APP1 asks for its TOP_INFOR page
        and server address (6101 -> 6401 -> 6501 -> 1004).
        """

        host, owner = connection.host, connection.owner
        advertise_host = _resolve_advertise_host(
            configured_host=self._config.advertise_host,
            bind_host=self._config.game_bind_host,
            client_host=host,
        )
        while True:
            request = connection.receive()
            command = frame_command(request)
            if command == CMD_APP_WORLD:
                connection.phase = AppPhase.WORLD
                self._flows.advance(host, owner, AppPhase.WORLD)
            elif command in (CMD_APP_SERVER_ADDRESS, CMD_APP_WORLD_STATUS) and connection.phase is AppPhase.WORLD:
                connection.phase = AppPhase.LAND
                self._flows.advance(host, owner, AppPhase.LAND)
            elif command == CMD_APP_COMPLETE:
                connection.reply(request)
                if connection.phase is AppPhase.APP1:
                    pass
                elif connection.phase is AppPhase.WORLD:
                    self._flows.advance(host, owner, AppPhase.POSTWORLD)
                elif connection.phase is AppPhase.POSTWORLD:
                    self._flows.advance(host, owner, AppPhase.LAND)
                else:
                    self._flows.release(host, owner)
                connection.linger(PHASE_CLOSE_LINGER_SECONDS)
                return

            sequence = frame_sequence(request)
            try:
                reply = self._reply(request, connection, advertise_host)
            except ValueError as exc:
                _LOGGER.warning('Rejecting APP command 0x%04x: %s', command, exc)
                connection.send(app_frame(APP_DIRECTION_REPLY, command, sequence=sequence, error=0xFF))
                continue
            if reply is None:
                _LOGGER.warning('Unexpected APP %s command 0x%04x; not answered.', connection.phase, command)
                continue
            connection.reply(request, reply)

    def _reply(self, request: bytes, connection: '_AppConnection', advertise_host: str) -> bytes | None:
        command = frame_command(request)
        sequence = frame_sequence(request)
        payload = frame_payload(request)
        phase = connection.phase

        def encode_field(data: bytes) -> bytes:
            return encode_app_field(data, sequence)

        def decode_field(data: bytes) -> tuple[bytes, int]:
            return decode_app_field(data, sequence)

        if command == CMD_APP_WORLD:
            return b'\x00'
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
            return struct.pack('>8H', *CONNECTION_TIMING_FRAMES)
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
            # No server-side resource after a quest return.
            return b'\x00'
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
            self._store_quest_record(payload, connection)
            return b''
        return None

    def _store_quest_record(self, payload: bytes, connection: '_AppConnection') -> None:
        """Keep one 6307 quest record for the connection's player.

        The acknowledgement does not depend on it: the client only waits for
        the empty reply before it adds the counts to its save.
        """

        record = decode_quest_record(payload)
        session_id = self._flows.attribute(connection.host, connection.owner)
        session = None if session_id is None else self._session(session_id)
        if session is None:
            _LOGGER.warning(
                'Quest record from %s has no single logged-in player; not stored: %s', connection.host, record
            )
            return
        store_quest_record(self._records, self._config.profile.identifier, session, record)
        _LOGGER.info('Stored quest record for %s: %s', session.username, record)

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
        populations = []
        for key in keys:
            land = None if world is not self._directory.local_world else world.land_by_key(key)
            populations.append(0 if land is None else self._land_population(land))
        return struct.pack(f'>B{len(populations)}H', len(populations), *populations) + world_field


class _AppConnection:
    """Framed request/reply helper around one client socket."""

    def __init__(self, client: socket.socket, phase: AppPhase, host: str, owner: int | None) -> None:
        self._client = client
        self._buffer = bytearray()
        self.phase = phase
        self.host = host
        self.owner = owner
        # Client build from the 1002 handshake (`APP_BUILD_NA` / `APP_BUILD_EU`).
        self.build = 0

    def poll(self, seconds: float) -> bool:
        """Return whether a complete frame arrives within `seconds` (it stays buffered)."""

        deadline = time.monotonic() + seconds
        previous = self._client.gettimeout()
        try:
            while not self._has_frame():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._client.settimeout(remaining)
                try:
                    data = self._client.recv(65535)
                except socket.timeout:
                    return False
                if not data:
                    raise ConnectionError('client closed the connection')
                self._buffer.extend(data)
            return True
        finally:
            self._client.settimeout(previous)

    def peek_command(self) -> int:
        """Command of the buffered frame `poll` reported."""

        return int.from_bytes(self._buffer[3:5], 'big')

    def _has_frame(self) -> bool:
        return len(self._buffer) >= 8 and len(self._buffer) >= 8 + int.from_bytes(self._buffer[:2], 'big')

    def receive(self) -> bytes:
        while (frame := pop_app_frame(self._buffer)) is None:
            data = self._client.recv(65535)
            if not data:
                raise ConnectionError('client closed the connection')
            self._buffer.extend(data)
        _LOGGER.debug('APP %s C->S 0x%04x %s', self.phase, frame_command(frame), frame.hex(' '))
        return frame

    def send(self, frame: bytes) -> None:
        _LOGGER.debug('APP %s S->C 0x%04x %s', self.phase, frame_command(frame), frame.hex(' '))
        self._client.sendall(frame)

    def reply(self, request: bytes, payload: bytes = b'') -> None:
        self.send(
            app_frame(APP_DIRECTION_REPLY, frame_command(request), sequence=frame_sequence(request), payload=payload)
        )

    def linger(self, seconds: float) -> None:
        """Give the client time to close first after the final reply."""

        self._client.settimeout(0.05)
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            try:
                if not self._client.recv(65535):
                    return
            except socket.timeout:
                continue
