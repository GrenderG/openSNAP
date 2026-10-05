"""World, Land and Area directory published to Monster Hunter clients.

Worlds and Lands are listed over APP: 6503 pages the World list, then each
World's Lands are paged with 6504 (keyed by the World key) and their
populations read with 6510. The World key is that World's game server host:
the client resolves it when the World is chosen (`lobby.bin` `0x00613260`)
and logs in to the resulting address (`0x0061b3b8`). The World with an empty
host is served here; any other World is hosted elsewhere and only listed.

Areas are SNAP lobbies named `<land key><NN>`: the client asks for one Land's
Areas with a `NAME` range from `<key>01` to `<key>26` (`0x00612760`) and takes
the Area number from the last two name digits (`0x00612c20`).
"""

from dataclasses import dataclass
import json
import os
import string

# 6503 World list (`0x002b1c60`): at most 16 Worlds in 608-byte records.
MAX_WORLDS = 16
WORLD_HOST_MAX_SIZE = 62
WORLD_NAME_MAX_SIZE = 15
WORLD_DESCRIPTION_MAX_SIZE = 255
# `<key>01`/`<key>26` are built in 16-byte buffers (`0x006128e8`), so the key
# leaves room for two digits and the terminator.
LAND_KEY_MAX_SIZE = 13
LAND_NAME_MAX_SIZE = 15
LAND_DESCRIPTION_MAX_SIZE = 255
# 6504 (`0x002b2840`) keeps at most 64 Lands in total; each World record holds
# 56 Land pointers (`+384` to the end of its 608 bytes).
MAX_LANDS = 64
MAX_LANDS_PER_WORLD = 56
# Land capacity is a u16 compared with the summed Area populations (`0x00612b14`).
MAX_LAND_CAPACITY = 0xFFFF
DEFAULT_LAND_CAPACITY = 750
# Area queries cover `<key>01..<key>26` with a 26-record limit.
MAX_AREAS_PER_LAND = 26
# Town directory callback (`0x006173a0`): three Town categories (rules bits
# 24-26) of 21 rows each.
TOWN_CATEGORY_COUNT = 3
TOWNS_PER_CATEGORY = 21
MAX_TOWNS_PER_AREA = TOWN_CATEGORY_COUNT * TOWNS_PER_CATEGORY

WORLDS_ENVIRONMENT_KEY = 'OPENSNAP_MH_WORLDS'
# The World and Land menus show the highlighted entry's description in their
# message box (`0x0060bad0`: World `+82` at `0x005f1e8c`, Land `+61` at
# `0x00602758`); these fill it when a World or Land leaves it out.
DEFAULT_WORLD_DESCRIPTION = 'Select a world to login to.'
DEFAULT_LAND_DESCRIPTION = 'Select a land to login to.'
# Original service layout: Brave World with the Red, Green and Blue Lands.
# Colors are the matching entries of the JP release's hardcoded Land panel
# table (`SLPM_654.95` `netr_sub01_col`, read by `Get_ServerColor`), which NA
# replaced with the server-provided word.
DEFAULT_WORLDS = {
    'Brave World': {
        'lands': [
            {'key': 'RED', 'name': 'Red', 'color': '#FF5D5D', 'areas': 2},
            {'key': 'GREEN', 'name': 'Green', 'color': '#73CB8D', 'areas': 2},
            {'key': 'BLUE', 'name': 'Blue', 'color': '#96B5FD', 'areas': 2},
        ],
    },
}


@dataclass(frozen=True, slots=True)
class Area:
    """One Area (SNAP lobby) of a Land."""

    area_id: int
    name: str
    land_number: int


@dataclass(frozen=True, slots=True)
class Land:
    """One Land of a World's 6504 list."""

    number: int
    key: str
    name: str
    description: str
    color: int
    capacity: int
    areas: tuple[Area, ...]


@dataclass(frozen=True, slots=True)
class World:
    """One World of the 6503 list; an empty host means this server."""

    host: str
    name: str
    description: str
    lands: tuple[Land, ...]

    def land_by_key(self, key: str) -> Land | None:
        return next((land for land in self.lands if land.key == key), None)


class Directory:
    """Configured Worlds; Areas exist only for the World served here."""

    def __init__(self, worlds: tuple[World, ...], *, max_players_per_town: int) -> None:
        self.worlds = worlds
        self.local_world = next(world for world in worlds if not world.host)
        self.lands = self.local_world.lands
        self.areas = tuple(area for land in self.lands for area in land.areas)
        self._areas_by_id = {area.area_id: area for area in self.areas}
        self._lands_by_number = {land.number: land for land in self.lands}
        # The client never reads an Area capacity; the server caps an Area at
        # every Town filled to the room maximum.
        self.area_capacity = MAX_TOWNS_PER_AREA * max_players_per_town

    def area(self, area_id: int | None) -> Area | None:
        return None if area_id is None else self._areas_by_id.get(area_id)

    def land_of(self, area: Area) -> Land:
        return self._lands_by_number[area.land_number]

    def world_by_host(self, host: str, default_host: str) -> World | None:
        return next((world for world in self.worlds if (world.host or default_host) == host), None)

    def areas_in_name_range(self, low: str, high: str) -> tuple[Area, ...]:
        return tuple(area for area in self.areas if low <= area.name <= high)


def read_directory(*, max_players_per_town: int) -> Directory:
    """Build the directory from `OPENSNAP_MH_WORLDS`: World name -> World object, in list order."""

    raw = os.getenv(WORLDS_ENVIRONMENT_KEY, '').strip()
    specs = _parse_worlds(raw) if raw else DEFAULT_WORLDS
    if not 1 <= len(specs) <= MAX_WORLDS:
        raise ValueError(f'{WORLDS_ENVIRONMENT_KEY} must define between 1 and {MAX_WORLDS} Worlds.')

    worlds = []
    area_id = 1
    for name, spec in specs.items():
        _require_object(spec, 'World')
        if not isinstance(name, str) or not name.isascii() or not 0 < len(name) <= WORLD_NAME_MAX_SIZE:
            raise ValueError(f'World names must be ASCII text of 1 to {WORLD_NAME_MAX_SIZE} characters.')
        land_specs = spec.get('lands')
        if not isinstance(land_specs, list) or not 1 <= len(land_specs) <= MAX_LANDS_PER_WORLD:
            raise ValueError(f'Each World must list between 1 and {MAX_LANDS_PER_WORLD} Lands.')
        host = _text(spec, 'host', WORLD_HOST_MAX_SIZE)
        enabled = spec.get('enabled', True)
        if type(enabled) is not bool:
            raise ValueError(f'World {name!r}: enabled must be true or false.')
        lands = []
        for number, land_spec in enumerate(land_specs, start=1):
            land = _read_land(land_spec, number, area_id)
            lands.append(land)
            if enabled and not host:
                area_id += len(land.areas)
        if len({land.key for land in lands}) != len(lands):
            raise ValueError('Land keys must be unique within a World.')
        # Disabled Worlds are validated but not published.
        if not enabled:
            continue
        worlds.append(
            World(
                host=host,
                name=name,
                description=_text(spec, 'description', WORLD_DESCRIPTION_MAX_SIZE, default=DEFAULT_WORLD_DESCRIPTION),
                lands=tuple(lands),
            )
        )

    if not worlds:
        raise ValueError(f'{WORLDS_ENVIRONMENT_KEY} must enable at least one World.')
    if sum(len(world.lands) for world in worlds) > MAX_LANDS:
        raise ValueError(f'Worlds may list at most {MAX_LANDS} Lands in total.')
    # 6504/6510 select an enabled World by its key (host), and exactly one is served here.
    hosts = [world.host for world in worlds]
    if len(set(hosts)) != len(hosts) or hosts.count('') != 1:
        raise ValueError('Exactly one World must leave its host empty, and World hosts must be unique.')
    return Directory(tuple(worlds), max_players_per_town=max_players_per_town)


def _parse_worlds(raw: str) -> dict:
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f'{WORLDS_ENVIRONMENT_KEY} is not valid JSON: {exc}') from None
    if not isinstance(parsed, dict):
        raise ValueError(f'{WORLDS_ENVIRONMENT_KEY} must be an object of World name -> World.')
    return parsed


def _read_land(spec: object, number: int, first_area_id: int) -> Land:
    _require_object(spec, 'Land')
    key = _text(spec, 'key', LAND_KEY_MAX_SIZE, required=True)
    area_count = spec.get('areas', 1)
    if type(area_count) is not int or not 1 <= area_count <= MAX_AREAS_PER_LAND:
        raise ValueError(f'Land {key!r}: areas must be an integer from 1 to {MAX_AREAS_PER_LAND}.')
    capacity = spec.get('capacity', DEFAULT_LAND_CAPACITY)
    if type(capacity) is not int or not 1 <= capacity <= MAX_LAND_CAPACITY:
        raise ValueError(f'Land {key!r}: capacity must be an integer from 1 to {MAX_LAND_CAPACITY}.')
    return Land(
        number=number,
        key=key,
        name=_text(spec, 'name', LAND_NAME_MAX_SIZE),
        description=_text(spec, 'description', LAND_DESCRIPTION_MAX_SIZE, default=DEFAULT_LAND_DESCRIPTION),
        color=_color(spec),
        capacity=capacity,
        areas=tuple(
            # Area id 0 means "not in an Area" to the client (`0x00612050`).
            Area(area_id=first_area_id + index, name=f'{key}{index + 1:02d}', land_number=number)
            for index in range(area_count)
        ),
    )


def _require_object(spec: object, kind: str) -> None:
    if not isinstance(spec, dict):
        raise ValueError(f'Each {kind} entry must be a JSON object.')


def _text(spec: dict, field: str, max_size: int, *, required: bool = False, default: str = '') -> str:
    value = spec.get(field, default)
    if not isinstance(value, str) or not value.isascii() or len(value) > max_size or (required and not value):
        raise ValueError(f'{field} must be ASCII text of at most {max_size} characters.')
    return value


def _color(spec: dict) -> int:
    # Land menu panel tint (`lobby.bin` `0x006022d8`): the client ORs alpha
    # `0x50` into the top byte, so only `0xRRGGBB` is configurable.
    value = spec.get('color', '#000000')
    if not isinstance(value, str) or len(value) != 7 or value[0] != '#' or not all(c in string.hexdigits for c in value[1:]):
        raise ValueError('Land color must be a "#RRGGBB" string.')
    return int(value[1:], 16)
