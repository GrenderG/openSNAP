"""Resident Evil Outbreak File#2 server and Area directory.

File#2 has no built-in Area names. On login `netwk.bin` (NA `SLUS_209.84`,
loaded at `0x0058d000`; EU `SLES_533.19` at `0x005ac300`) asks for the Areas
of the server it downloaded over the Capcom APP service: `CMD_QUERY_AREA` with
`NAME` between `<key>01` and `<key>10`, limit 10 (NA `0x0059c954`,
`0x005a0f34`, `0x005b5394`; EU `0x005bcb84`, `0x005c1174`, `0x005c70a4`),
where `<key>` is the Land key of the APP 6504 reply. The Area number is the
last two digits of the name (NA `0x005a1058`), 1..10. The Friend status reads
those digits at `name[4:6]` (NA `0x005b3b50`), so the key is exactly four
characters. `OID == id` looks one Area up for the Friend search (NA
`0x005a3edc`).

Capacity is one number for the whole server (APP 6504 sub-record `+298`): the
login sums the users of the ten Areas and refuses to enter when the sum
reaches it (NA `0x0059c730`, "Server full.").

What each Area offers (its title, description, difficulty and ranking
category) is not SN@P data: the APP service sends it in its data file
(`opensnap_app.capcom.outbreak2.netbio`), built from the same settings.
"""

from dataclasses import dataclass
import json
import os

AREA_COUNT = 10
AREA_KEY_LENGTH = 4
DEFAULT_AREA_KEY = 'AREA'
DEFAULT_SERVER_NAME = 'openSNAP'
DEFAULT_SERVER_DESCRIPTION = 'Resident Evil Outbreak File#2 on openSNAP.'
DEFAULT_CAPACITY = 400
# APP 6504 sends the capacity as a `u16` (`0x001fc440`).
MAX_CAPACITY = 0xFFFF
# APP 6503/6504 field limits (NA `0x001fba80`, `0x001fc440`): World key 62,
# names 15, descriptions 255.
MAX_NAME_LENGTH = 15
MAX_DESCRIPTION_LENGTH = 255
# Area record limits (`netwk.bin` copies 24-byte titles and a 256-byte
# description, `0x005a0e64`, `0x005a0e7c`).
MAX_AREA_TITLE_LENGTH = 23
MAX_AREA_DESCRIPTION_LENGTH = 255

# The room difficulty an Area fixes (built-in rule "Difficulty", choices
# EASY..VERY HARD = 0..3), or `HOST_DIFFICULTY` to let the room host choose.
DIFFICULTIES = ('easy', 'normal', 'hard', 'very hard')
HOST_DIFFICULTY = 'host'
# "rank kind" (0..7) is the room rule the result reports carry as their
# ranking category (`opensnap.plugins.outbreak2.records`).
RANK_KINDS = 8


@dataclass(frozen=True, slots=True)
class Area:
    """One Area; `area_id` is the SN@P lobby id and `number` the 1..10 the client shows."""

    area_id: int
    number: int
    name: str
    title: str
    subtitle: str
    description: str
    difficulty: str
    rank_kind: int

    @property
    def fixed_difficulty(self) -> int | None:
        """The Difficulty choice the Area fixes, or None when the host chooses."""

        return None if self.difficulty == HOST_DIFFICULTY else DIFFICULTIES.index(self.difficulty)


@dataclass(frozen=True, slots=True)
class Directory:
    """This server as the lobby lists it: one server with ten Areas."""

    server_name: str
    server_description: str
    area_key: str
    capacity: int
    areas: tuple[Area, ...]

    def area(self, area_id: int) -> Area | None:
        return next((area for area in self.areas if area.area_id == area_id), None)

    def rank_kind_names(self) -> dict[int, str]:
        """Each rank kind the Areas use, labelled by the subtitle (else the title) of its first Area."""

        names: dict[int, str] = {}
        for area in self.areas:
            names.setdefault(area.rank_kind, area.subtitle or area.title or f'Rank {area.rank_kind}')
        return names

    def in_name_range(self, low: str, high: str) -> tuple[Area, ...]:
        return tuple(area for area in self.areas if low <= area.name <= high)


# Two Areas per difficulty plus two where the host chooses; rank kinds 0..3
# are the difficulties and 4 the host's choice, whose board therefore mixes
# difficulties. The rankings label each rank kind with the subtitle of the
# first Area using it.
DEFAULT_AREAS: tuple[dict[str, object], ...] = tuple(
    {
        'title': title,
        'subtitle': subtitle,
        'description': description,
        'difficulty': difficulty,
        'rank_kind': rank_kind,
    }
    for title, subtitle, description, difficulty, rank_kind in (
        ('Easy 1', 'EASY', '<BODY>Rooms play on EASY.<END>', 'easy', 0),
        ('Easy 2', 'EASY', '<BODY>Rooms play on EASY.<END>', 'easy', 0),
        ('Normal 1', 'NORMAL', '<BODY>Rooms play on NORMAL.<END>', 'normal', 1),
        ('Normal 2', 'NORMAL', '<BODY>Rooms play on NORMAL.<END>', 'normal', 1),
        ('Hard 1', 'HARD', '<BODY>Rooms play on HARD.<END>', 'hard', 2),
        ('Hard 2', 'HARD', '<BODY>Rooms play on HARD.<END>', 'hard', 2),
        ('Very Hard 1', 'VERY HARD', '<BODY>Rooms play on VERY HARD.<END>', 'very hard', 3),
        ('Very Hard 2', 'VERY HARD', '<BODY>Rooms play on VERY HARD.<END>', 'very hard', 3),
        ('Free Rules 1', 'ANY', '<BODY>The room host picks the difficulty.<END>', HOST_DIFFICULTY, 4),
        ('Free Rules 2', 'ANY', '<BODY>The room host picks the difficulty.<END>', HOST_DIFFICULTY, 4),
    )
)


def build_directory(
    *,
    server_name: str = DEFAULT_SERVER_NAME,
    server_description: str = DEFAULT_SERVER_DESCRIPTION,
    area_key: str = DEFAULT_AREA_KEY,
    capacity: int = DEFAULT_CAPACITY,
    areas: tuple[dict[str, object], ...] = DEFAULT_AREAS,
) -> Directory:
    """Validate the settings and name the ten Areas `<key>01`..`<key>10`."""

    if len(area_key) != AREA_KEY_LENGTH or not area_key.isascii() or not area_key.isalnum():
        raise ValueError(f'Outbreak File#2 Area key must be {AREA_KEY_LENGTH} ASCII letters or digits, got {area_key!r}.')
    if not 0 < len(server_name) <= MAX_NAME_LENGTH or not server_name.isascii():
        raise ValueError(f'Outbreak File#2 server name must be 1-{MAX_NAME_LENGTH} ASCII characters, got {server_name!r}.')
    if len(server_description) > MAX_DESCRIPTION_LENGTH or not server_description.isascii():
        raise ValueError(f'Outbreak File#2 server description must be ASCII, at most {MAX_DESCRIPTION_LENGTH} characters.')
    if not 1 <= capacity <= MAX_CAPACITY:
        raise ValueError(f'Outbreak File#2 capacity must be 1-{MAX_CAPACITY}, got {capacity}.')
    if len(areas) != AREA_COUNT:
        raise ValueError(f'Outbreak File#2 needs exactly {AREA_COUNT} Areas, got {len(areas)}.')
    return Directory(
        server_name=server_name,
        server_description=server_description,
        area_key=area_key,
        capacity=capacity,
        areas=tuple(_area(number, area_key, settings) for number, settings in enumerate(areas, start=1)),
    )


def _area(number: int, area_key: str, settings: dict[str, object]) -> Area:
    title = _text(settings, 'title', MAX_AREA_TITLE_LENGTH, number)
    subtitle = _text(settings, 'subtitle', MAX_AREA_TITLE_LENGTH, number)
    description = _text(settings, 'description', MAX_AREA_DESCRIPTION_LENGTH, number)
    difficulty = str(settings.get('difficulty', HOST_DIFFICULTY)).strip().lower()
    if difficulty not in DIFFICULTIES and difficulty != HOST_DIFFICULTY:
        raise ValueError(f'Outbreak File#2 Area {number}: difficulty must be one of {DIFFICULTIES} or {HOST_DIFFICULTY!r}.')
    rank_kind = settings.get('rank_kind', 0)
    if not isinstance(rank_kind, int) or not 0 <= rank_kind < RANK_KINDS:
        raise ValueError(f'Outbreak File#2 Area {number}: rank_kind must be 0-{RANK_KINDS - 1}.')
    return Area(
        area_id=number,
        number=number,
        name=f'{area_key}{number:02d}',
        title=title,
        subtitle=subtitle,
        description=description,
        difficulty=difficulty,
        rank_kind=rank_kind,
    )


def _text(settings: dict[str, object], key: str, limit: int, number: int) -> str:
    """ASCII only: the NA build has no UTF-8 table and both regions read the same file."""

    value = str(settings.get(key, ''))
    if len(value) > limit or not value.isascii():
        raise ValueError(f'Outbreak File#2 Area {number}: {key} must be ASCII, at most {limit} characters.')
    return value


def read_directory() -> Directory:
    """Build the directory from the `OPENSNAP_OUTBREAK2_*` settings."""

    areas_setting = os.getenv('OPENSNAP_OUTBREAK2_AREAS', '').strip()
    areas = DEFAULT_AREAS
    if areas_setting:
        parsed = json.loads(areas_setting)
        if not isinstance(parsed, list) or not all(isinstance(item, dict) for item in parsed):
            raise ValueError('OPENSNAP_OUTBREAK2_AREAS must be a JSON list of Area objects.')
        areas = tuple(parsed)
    return build_directory(
        server_name=os.getenv('OPENSNAP_OUTBREAK2_SERVER_NAME', '').strip() or DEFAULT_SERVER_NAME,
        server_description=os.getenv('OPENSNAP_OUTBREAK2_SERVER_DESCRIPTION', '').strip() or DEFAULT_SERVER_DESCRIPTION,
        area_key=os.getenv('OPENSNAP_OUTBREAK2_AREA_KEY', '').strip() or DEFAULT_AREA_KEY,
        capacity=int(os.getenv('OPENSNAP_OUTBREAK2_CAPACITY', '').strip() or DEFAULT_CAPACITY),
        areas=areas,
    )
