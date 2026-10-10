"""Title/region profiles for Monster Hunter SNAP clients."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class MemberProfileLayout:
    """The member profile a build publishes (`CMD_CHANGE_USER_PROPERTY`) and copies from rosters.

    `size` is also the profile size the build passes to `kkLoginClient` (`t3`).
    Before a quest a guest publishes `3/5` at the two departure offsets (see
    the plugin's quest departure notes).
    """

    size: int
    party_state_offset: int
    quest_state_offset: int


@dataclass(frozen=True, slots=True)
class MonsterHunterProfile:
    """Values that differ between Monster Hunter releases.

    Everything else in the plugin is shared by all Monster Hunter titles that
    run the same SNAP lobby overlay.
    """

    # Game plugin name and `data/<data_directory>` folder for APP resources.
    identifier: str
    data_directory: str
    # Environment variable holding the World/Land/Area directory.
    worlds_environment_key: str
    member_profile: MemberProfileLayout
    # 6213 reply: how many of the eight lobby timing words the client reads.
    connection_timing_words: int
    # How the Friend status reads the Area name of its `OID` Area lookup.
    # True: it prints `name[4:-2]` as the Land name and the last two digits as
    # the Area letter (NA `lobby.bin` `0x00612118..0x00612210`, EU
    # `0x005bf7c0`). False: it looks `name[:-2]` up among the Land keys and
    # prints that Land's name (beta `0x005b2bc0`).
    status_area_name_skips_prefix: bool


# `SLUS_208.96` (Monster Hunter NTSC-U), and the PAL builds sharing its lobby
# protocol. Profile: 24 bytes from `0x0074f4d8` and 116 from `0x0074f464`, with
# the departure state at `0x0074f4d4/5` (`lobby.bin` `0x0061cb40`).
MONSTER_HUNTER_NA = MonsterHunterProfile(
    identifier='monsterhunter',
    data_directory='monsterhunter',
    worlds_environment_key='OPENSNAP_MH_WORLDS',
    member_profile=MemberProfileLayout(size=140, party_state_offset=136, quest_state_offset=137),
    connection_timing_words=8,
    status_area_name_skips_prefix=True,
)

# `SLUS_291.10` (Monster Hunter NTSC-U public beta, May 2004). Its profile is
# 64 bytes from `0x006b7830` and 152 from `0x006b7798`, with the departure
# state at `0x006b782d`/`0x006b782f` (`lobby.bin` `0x005bd190`), so it can't
# share rosters with the release. Its 6213 handler reads four words
# (`0x002ad090`): it has no lobby keepalive.
MONSTER_HUNTER_NA_BETA = MonsterHunterProfile(
    identifier='monsterhunter_na_beta',
    data_directory='monsterhunter_na_beta',
    worlds_environment_key='OPENSNAP_MH_NA_BETA_WORLDS',
    member_profile=MemberProfileLayout(size=216, party_state_offset=213, quest_state_offset=215),
    connection_timing_words=4,
    status_area_name_skips_prefix=False,
)
