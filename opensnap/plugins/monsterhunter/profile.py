"""Title/region profiles for Monster Hunter SNAP clients."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class MonsterHunterProfile:
    """Values that differ between Monster Hunter releases.

    Everything else in the plugin is shared by all Monster Hunter titles that
    run the same SNAP lobby overlay.
    """

    # Game plugin name and `data/<data_directory>` folder for APP resources.
    identifier: str
    data_directory: str


# `SLUS_208.96` (Monster Hunter NTSC-U).
MONSTER_HUNTER_NA = MonsterHunterProfile(
    identifier='monsterhunter',
    data_directory='monsterhunter',
)
