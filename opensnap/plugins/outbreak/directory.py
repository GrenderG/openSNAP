"""Resident Evil Outbreak Area directory.

`netwk.bin` (`SLUS_207.65`, loaded at `0x00570000`) finds its Areas by name
with `CMD_QUERY_AREA`:

- `NAME == "obmft"`: the free-mode hall, the client's Area 0 (`0x00588900`);
- `NAME` from `obms01` to `obms05`: the scenario-mode Areas;
- `NAME` from `obmf01` to `obmf99`: the free-mode Areas (`0x00588d18`);
- `OID == id`: one Area, for the Friend search (`0x0058adac`).

The client numbers an Area by the last two digits of its name (`0x00588a68`)
and keeps at most 99.
"""

from dataclasses import dataclass
import os

FREE_HALL_NAME = 'obmft'
SCENARIO_AREA_PREFIX = 'obms'
FREE_AREA_PREFIX = 'obmf'
SCENARIO_AREA_COUNT = 5
MAX_FREE_AREAS = 99
DEFAULT_FREE_AREAS = 10
DEFAULT_AREA_CAPACITY = 100


@dataclass(frozen=True, slots=True)
class Area:
    """One Area; `area_id` is the SN@P lobby id."""

    area_id: int
    name: str


@dataclass(frozen=True, slots=True)
class Directory:
    """Every Area this server publishes."""

    areas: tuple[Area, ...]
    capacity: int

    def area(self, area_id: int) -> Area | None:
        return next((area for area in self.areas if area.area_id == area_id), None)

    def named(self, name: str) -> Area | None:
        return next((area for area in self.areas if area.name == name), None)

    def in_name_range(self, low: str, high: str) -> tuple[Area, ...]:
        """Areas whose name sorts between `low` and `high` within the same family."""

        return tuple(
            area for area in self.areas
            if len(area.name) == len(low) and low <= area.name <= high
        )


def build_directory(free_areas: int, capacity: int) -> Directory:
    """The free-mode hall, the five scenario Areas and `free_areas` free-mode Areas."""

    if not 1 <= free_areas <= MAX_FREE_AREAS:
        raise ValueError(f'Outbreak free-mode Areas must be 1-{MAX_FREE_AREAS}, got {free_areas}.')
    if capacity < 1:
        raise ValueError(f'Outbreak Area capacity must be positive, got {capacity}.')
    names = (
        [FREE_HALL_NAME]
        + [f'{SCENARIO_AREA_PREFIX}{number:02d}' for number in range(1, SCENARIO_AREA_COUNT + 1)]
        + [f'{FREE_AREA_PREFIX}{number:02d}' for number in range(1, free_areas + 1)]
    )
    return Directory(
        areas=tuple(Area(area_id=index, name=name) for index, name in enumerate(names, start=1)),
        capacity=capacity,
    )


def read_directory() -> Directory:
    """Build the directory from `OPENSNAP_OUTBREAK_FREE_AREAS` and `OPENSNAP_OUTBREAK_AREA_CAPACITY`."""

    free_areas = int(os.getenv('OPENSNAP_OUTBREAK_FREE_AREAS', '').strip() or DEFAULT_FREE_AREAS)
    capacity = int(os.getenv('OPENSNAP_OUTBREAK_AREA_CAPACITY', '').strip() or DEFAULT_AREA_CAPACITY)
    return build_directory(free_areas, capacity)
