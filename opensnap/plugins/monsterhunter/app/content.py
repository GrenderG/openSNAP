"""Online content served through the Monster Hunter APP service."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
import hashlib
import json
import logging
from pathlib import Path
import struct

_LOGGER = logging.getLogger('opensnap.plugins.monsterhunter.app')

# Market day values (6211, stored by `0x002af0c0`). The Minegarde vendor
# dialog table (`lobby.bin` `0x00686490`) follows the same index order as the
# JP release: normal, materials (Claw), items (Tools), food, big sale.
# Values 5-7 are the half-price variants of 1-3 and are not part of the
# rotation.
MARKET_NORMAL = 0
MARKET_CLAW_DAY = 1
MARKET_TOOLS_DAY = 2
MARKET_FOOD_DAY = 3
MARKET_HALF_PRICE_DAY = 4
# Minegarde shop rotation used by the JP service, indexed by day of the year.
MARKET_ROTATION = (
    MARKET_CLAW_DAY,
    MARKET_NORMAL,
    MARKET_HALF_PRICE_DAY,
    MARKET_NORMAL,
    MARKET_TOOLS_DAY,
    MARKET_NORMAL,
    MARKET_HALF_PRICE_DAY,
    MARKET_NORMAL,
    MARKET_FOOD_DAY,
    MARKET_NORMAL,
)
EVENTS_DIRECTORY = 'events'
EVENT_MANIFEST_FILE = 'manifest.json'
EVENT_RESOURCE_MAX_SIZE = 0x8000
INFORMATION_FILE = 'information.json'
# `0x002af210`: title field up to 31 bytes, at most 3 pages of at most 8192 bytes.
INFORMATION_TITLE_MAX_SIZE = 31
INFORMATION_PAGE_COUNT_MAX = 3
INFORMATION_PAGE_MAX_SIZE = 8192
# The client always requests 754-byte resource chunks and advances by that
# size (`0x002af468` Information pages, `0x002afba0` Event).
RESOURCE_CHUNK_SIZE = 0x2F2
FILES_DIRECTORY = 'files'
# Named files (6101/6102) are downloaded in 722-byte chunks (EU `0x002a07fc`).
FILE_CHUNK_SIZE = 0x2D2


class MarketState:
    """Market day derived from the server's local date."""

    def __init__(self, today: Callable[[], date] = date.today) -> None:
        self._today = today

    def native_response(self) -> bytes:
        """Return the 6211 payload: one big-endian Market day word."""

        day_of_year = self._today().timetuple().tm_yday
        return struct.pack('>L', MARKET_ROTATION[(day_of_year - 1) % len(MARKET_ROTATION)])


@dataclass(frozen=True, slots=True)
class EventResource:
    """One downloadable Event quest."""

    quest_id: int
    title: str
    catalog_key: bytes
    content: bytes


class EventCatalog:
    """Event quests listed in `events/manifest.json`, one per day in index order.

    The rotation follows the Market's: the day of the year picks the entry, and
    the list repeats.
    """

    def __init__(self, data_directory: Path, today: Callable[[], date] = date.today) -> None:
        self._events = _load_events(data_directory / EVENTS_DIRECTORY)
        self._today = today

    def availability(self) -> bytes:
        """Return the 6212 payload."""

        return struct.pack('>L', int(bool(self._events)))

    def catalog(self, encode_field: Callable[[bytes], bytes]) -> bytes:
        """Return the 6203 payload."""

        event = self.current()
        if event is None:
            return b'\x00'
        _LOGGER.info('Event of the day: #%d %r.', event.quest_id, event.title)
        return b'\x01' + encode_field(event.catalog_key) + struct.pack('>HL', 1, len(event.content))

    def download(self, request: bytes, encode_field: Callable[[bytes], bytes]) -> bytes:
        """Return one 6204 chunk."""

        event = self.current()
        if event is None:
            raise ValueError('No Event is available.')
        return _resource_chunk((event.content,), request, encode_field)

    def current(self) -> EventResource | None:
        """Return today's Event, if any."""

        if not self._events:
            return None
        day_of_year = self._today().timetuple().tm_yday
        return self._events[(day_of_year - 1) % len(self._events)]


class InformationPages:
    """Online Information pages (6201 list, 6202 download).

    The WORLD connection only continues to the Event, Market and connection
    timing requests after a non-empty 6201 list (`0x002af290`), so one empty
    page is published when `information.json` provides none.
    """

    def __init__(self, data_directory: Path) -> None:
        self._title, self._pages = _load_information(data_directory)

    def listing(self, encode_field: Callable[[bytes], bytes]) -> bytes:
        """Return the 6201 payload: `u8 available, field title, u16 count, u32 sizes`."""

        sizes = b''.join(struct.pack('>L', len(page)) for page in self._pages)
        return b'\x01' + encode_field(self._title) + struct.pack('>H', len(self._pages)) + sizes

    def download(self, request: bytes, encode_field: Callable[[bytes], bytes]) -> bytes:
        """Return one 6202 chunk."""

        return _resource_chunk(self._pages, request, encode_field)


class NamedFiles:
    """Files the client asks for by name (6101 info, 6102 download).

    EU APP1 asks for `03/<language>/TOP_INFOR.HTM` (`0x002a0570`, language 1
    English, 2 French, 3 Italian, 4 Spanish, 5 German) and the EU lobby
    Information/Record menu for its `DATABASE.HTM` page (generated by the
    service from the records). Other names are looked up under `files/`. A missing file is published with size 0, which the client
    takes as nothing to show; a 6101 status of 0 would make its handler return
    an error (`0x002a06a0`).
    """

    def __init__(self, data_directory: Path, generated: Callable[[bytes], bytes | None] | None = None) -> None:
        self._root = data_directory / FILES_DIRECTORY
        # Server-generated pages (the Record page); a 6101 renders one and its
        # 6102 chunks are cut from that same snapshot.
        self._generated = generated
        self._snapshots: dict[bytes, bytes] = {}

    def info(
        self,
        request: bytes,
        decode_field: Callable[[bytes], tuple[bytes, int]],
        encode_field: Callable[[bytes], bytes],
    ) -> bytes:
        """Answer 6101 `u32, field name` with `u8 1, u32, field name, u32 size`.

        The client checks the name echo and stores the u32 without reading it.
        """

        if len(request) < 4:
            raise ValueError('File request is truncated.')
        name, _ = decode_field(request[4:])
        content = self._generated(name) if self._generated is not None else None
        if content is not None:
            self._snapshots[name] = content
        else:
            content = self._read(name)
        _LOGGER.info('APP file %r requested; %s.', name, 'missing' if content is None else f'{len(content)} bytes')
        return b'\x01' + bytes(4) + encode_field(name) + struct.pack('>L', 0 if content is None else len(content))

    def download(
        self,
        request: bytes,
        decode_field: Callable[[bytes], tuple[bytes, int]],
        encode_field: Callable[[bytes], bytes],
    ) -> bytes:
        """Answer 6102 `field name, u32 offset, u16 size` with `field name, u32 offset, field chunk`."""

        name, size = decode_field(request)
        if len(request) != size + 6:
            raise ValueError('Invalid file chunk request size.')
        offset, requested = struct.unpack_from('>LH', request, size)
        content = self._snapshots.get(name) or self._read(name)
        if content is None or requested != FILE_CHUNK_SIZE or offset % FILE_CHUNK_SIZE or offset >= len(content):
            raise ValueError('Invalid file name, offset, or chunk size.')
        return encode_field(name) + struct.pack('>L', offset) + encode_field(content[offset:offset + FILE_CHUNK_SIZE])

    def _read(self, name: bytes) -> bytes | None:
        root = self._root.resolve()
        path = (root / name.decode('ascii', errors='replace')).resolve()
        if root not in path.parents or not path.is_file():
            return None
        return path.read_bytes()


def _resource_chunk(resources: tuple[bytes, ...], request: bytes, encode_field: Callable[[bytes], bytes]) -> bytes:
    """Answer `u16 index, u32 offset, u16 requested` with `u16 index, u32 offset, field chunk`."""

    if len(request) != 8:
        raise ValueError('Invalid resource request size.')
    index, offset, requested = struct.unpack('>HLH', request)
    if index >= len(resources) or requested != RESOURCE_CHUNK_SIZE or offset % RESOURCE_CHUNK_SIZE:
        raise ValueError('Invalid resource index, offset, or chunk size.')
    content = resources[index]
    if offset and offset >= len(content):
        raise ValueError('Resource offset is past the end.')
    return struct.pack('>HL', index, offset) + encode_field(content[offset:offset + RESOURCE_CHUNK_SIZE])


def _load_information(data_directory: Path) -> tuple[bytes, tuple[bytes, ...]]:
    path = data_directory / INFORMATION_FILE
    if not path.exists():
        return b'', (b'',)
    try:
        config = json.loads(path.read_text(encoding='utf-8-sig'))
        title = str(config.get('title', '')).encode('ascii')
        pages = tuple((data_directory / name).read_bytes() for name in config.get('pages', []))
        if len(title) > INFORMATION_TITLE_MAX_SIZE:
            raise ValueError(f'title exceeds {INFORMATION_TITLE_MAX_SIZE} bytes')
        if not 1 <= len(pages) <= INFORMATION_PAGE_COUNT_MAX:
            raise ValueError(f'between 1 and {INFORMATION_PAGE_COUNT_MAX} pages are supported')
        if any(len(page) > INFORMATION_PAGE_MAX_SIZE for page in pages):
            raise ValueError(f'pages are limited to {INFORMATION_PAGE_MAX_SIZE} bytes')
    except (OSError, ValueError, TypeError, AttributeError, UnicodeError) as exc:
        _LOGGER.warning('Ignoring invalid %s; publishing one empty page: %s', path, exc)
        return b'', (b'',)
    return title, pages


def _load_events(events_directory: Path) -> tuple[EventResource, ...]:
    manifest_path = events_directory / EVENT_MANIFEST_FILE
    if not manifest_path.exists():
        return ()
    try:
        entries = json.loads(manifest_path.read_text(encoding='utf-8-sig'))['events']
        if sorted(entry['index'] for entry in entries) != list(range(len(entries))):
            raise ValueError('indexes must be 0..N-1 without gaps')
        root = events_directory.resolve()
        events = []
        for entry in sorted(entries, key=lambda item: item['index']):
            path = (root / entry['file']).resolve()
            if root not in path.parents:
                raise ValueError(f'{entry["file"]} leaves the events directory')
            content = path.read_bytes()
            if not 0 < len(content) <= EVENT_RESOURCE_MAX_SIZE:
                raise ValueError(f'{entry["file"]} must be 1..{EVENT_RESOURCE_MAX_SIZE} bytes')
            quest_id = int(entry['quest_id'])
            if _quest_number(content) != quest_id:
                raise ValueError(f'{entry["file"]} holds quest {_quest_number(content)}, not {quest_id}')
            events.append(EventResource(
                quest_id=quest_id,
                title=str(entry['title']),
                catalog_key=_event_catalog_key(quest_id, content),
                content=content,
            ))
    except (OSError, ValueError, KeyError, TypeError, AttributeError, struct.error) as exc:
        _LOGGER.warning('Event catalog disabled: %s', exc)
        return ()
    _LOGGER.info('Event catalog: %d quests in rotation.', len(events))
    return tuple(events)


def _quest_number(content: bytes) -> int:
    """Return the quest number: u16 at quest information +0x1e (NA `0x006cb78c`)."""

    (info,) = struct.unpack_from('<L', content, 0)
    return struct.unpack_from('<H', content, info + 0x1E)[0]


def _event_catalog_key(quest_id: int, content: bytes) -> bytes:
    """Return a key unique to this quest file.

    The client skips the download when the 6203 key matches the one it kept
    from the last Event (NA `0x002afa50` compares 31 bytes with `0x0055299c`).
    """

    return f'{quest_id}-{hashlib.sha256(content).hexdigest()[:24]}'.encode('ascii')
