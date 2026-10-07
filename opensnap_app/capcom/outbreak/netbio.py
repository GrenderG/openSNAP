"""Resident Evil Outbreak NETBIO data files, downloaded on each lobby entry (APP 6201/6202).

The client asks for them after its information page (main `0x001f5900`) and
keeps them in three 8 KB slots (`0x00357ad0`, pointers `0x0035ec78`, set at
`0x001f5af0`). Without them the lobby has no scenario descriptions and the
room rules screen shows every rule as fixed ("Unadjustable"). openSNAP builds
both files here; the layout is the one `netwk.bin` reads, the same in v1 and v2
(only main ELF addresses move), and the values are those of the alpha-server
pcaps (`etc/experiments/outbreak-file1-test-pcaps`) unless noted. Addresses are
v2.

File 0, the scenario table, is copied to the network work area (`+0x15540`,
`0x0057b700`) and read through `0x0057fb10`:

    48 x 72-byte scenario records, then at 0xd80:
    u8[3] team level thresholds, u8 disc note flag, 36 unread bytes,
    3 x char[32] disc titles, then at 0xe08 the description texts

- A record is `u8 number, u8 disc, u8 game scenario, char[32] name,
  u8 count + u8[16] scenes, u8 count + u8[15] final scenes, i32 description
  offset` (`0x0057fb30`..`0x0057fd30`). A negative offset makes the record
  absent. The lobby lists records 0..4 (`0x00578b8c`), so later records are
  absent. `number` is not read. `disc` picks the disc icon (`0x005d05d8`), and
  `game scenario` is the scenario the game loads (`0x005ef0f4`).
- Scenes are 1-based indexes into the five-scene table `0x0061dbe0`
  (`r0010150`, `r0350150`, `r0280850`, `r0022650`, `r0411350`), loaded through
  main `0x001d4c40` (`0x005757a0`). `0x0057fd30` draws one at random from the
  record's scenes, skipping those the save's rotation for the scenario
  already used (save `+0x4154`, 12 bytes per scenario, kept by submain
  `0x0037c5ec`: a rotation of 3 to 5 runs). The last run of a rotation draws
  from the final scenes instead.
- A team's level is the number of thresholds that every member's
  per-scenario profile counter reaches (`0x0057ff60`, the lowest member
  counts). It is stored next to the game options at game start (work
  `+0x4a2`).
- The disc note flag shows the disc icons and the note `Note: This is the
  data for "Disc <title>."` (`0x005f6780`, `0x005d5f98`). The title is picked
  by the disc index, which is always 0 here (main `0x001bac30`). The alpha
  files hold Japanese product codes in the 36 unread bytes and the Japanese
  disc titles. openSNAP writes the US title.
- Descriptions are NUL-terminated, in the game's own message markup (`<BODY>`
  starts a line, `<BR>` ends one, `<END>` ends the text), and are shown in the
  lobby (`0x0057fb90`).

File 1, the room rules, is read through `*0x0035ec7c` (`0x0058a630`..
`0x0058a880`). Rules are the room screen's rows: No. of Players (2, 3, 4),
Waiting Time (5, 10, 15, 30 min), Difficulty (EASY..VERY HARD) and Friendly
Fire (Off, On) (names `0x0060c760`, choices `0x0060c7b0`):

    u8[4] adjustable, u8[4] enabled, u8[4] defaults,
    u8[3] scenario-mode options, u8[3] free-mode options,
    i16 scenario-mode waiting seconds, u8 room name editable,
    u8 password editable, char[15] room name, char[8] password

- `adjustable` lets the host change a rule. A fixed rule keeps its default
  choice index from `defaults`, and the screen shows it as fixed.
- `enabled` is read for Difficulty and Friendly Fire only: an enabled rule
  passes the room's choice into the game options (`0x0058a8fc`), and an
  enabled Friendly Fire also adds the fourth row (`0x0058a658`).
- The options are `mode, difficulty, friendly fire`, copied into the game
  start options for the room's mode (`0x00599ba0`, `0x0058b080`). `mode` is 0
  for scenario mode and 1 for free mode.
- The waiting seconds are scenario mode's join timer, counted in frames at 30
  per second (`0x005894bc`). Two captures had 30 and the latest had 120.
- Without `room name editable` or `password editable`, rooms take the fixed
  room name or password (`0x005892c0`, `0x005895e4`).
"""

from dataclasses import dataclass
import struct

# 6201 `field` -> `u8 flag, field name, u16 count, count x u32 size`
# (`0x001f59c0`): the client keeps up to 31 name bytes, sent NUL-padded as in
# the pcaps, and 3 files.
NETBIO_NAME = b'BASLUS-20765NETBIO'.ljust(31, b'\x00')
FILE_SIZE = 0x2000

RECORD_COUNT = 48
MAX_SCENES = 16
MAX_FINAL_SCENES = 15
DISC_BLOCK_OFFSET = 0xD80
DISC_TITLES_OFFSET = 0xDA8
DESCRIPTIONS_OFFSET = 0xE08
ABSENT = -1

TEAM_LEVEL_THRESHOLDS = (0, 6, 31)
DISC = 1
DISC_TITLE = 'RESIDENT EVIL OUTBREAK'

SCENARIO_MODE_WAITING_SECONDS = 120


@dataclass(frozen=True, slots=True)
class Scenario:
    """One scenario record and its description."""

    name: str
    scenes: tuple[int, ...]
    final_scenes: tuple[int, ...]
    description: tuple[str, ...]


SCENARIOS = (
    Scenario(
        'Outbreak',
        (2, 3, 4, 2, 3, 4, 2, 3, 4),
        (5, 5),
        (
            "It was a typical night at J's bar.",
            'Some uninvited guests crashed the party.',
            'Our race for survival was just beginning.',
        ),
    ),
    Scenario(
        'Below Freezing Point',
        (3, 4, 3, 4, 3),
        (5,),
        (
            'We escaped the zombies and found our way into',
            'a frozen underground facility. What truths',
            'lie beyond the cries of pain in the distance?',
        ),
    ),
    Scenario(
        'The Hive',
        (4, 2),
        (5, 5),
        (
            'A hospital transformed into some kind of hive',
            'full of squirming "things." We pushed on and',
            'pushed the repulsive image from our minds.',
        ),
    ),
    Scenario(
        'Hellfire',
        (2, 3, 2, 3, 2),
        (5, 5),
        (
            'While avoiding blasts of flame, we proceeded',
            'through the smoke-encased hotel.',
            'We now knew the true meaning of "Hell."',
        ),
    ),
    Scenario(
        'Decisions, Decisions',
        (5,),
        (5,),
        (
            'Destruction. Darkness. We quickly raced forward',
            'knowing all too well that each decision we',
            'made held newfound hope or endless despair.',
        ),
    ),
)


@dataclass(frozen=True, slots=True)
class Rule:
    """One room rule (see the module notes)."""

    adjustable: bool
    enabled: bool
    default: int


# No. of Players, Waiting Time, Difficulty, Friendly Fire. The US manual's
# room rules screen (free mode, p. 25) has no Friendly Fire row; Capcom
# enabled it later, as the alpha files do. Off by default.
RULES = (
    Rule(adjustable=True, enabled=False, default=0),
    Rule(adjustable=True, enabled=False, default=0),
    Rule(adjustable=True, enabled=True, default=0),
    Rule(adjustable=True, enabled=True, default=0),
)
# `mode, difficulty, friendly fire`.
SCENARIO_MODE_OPTIONS = (0, 0, 0)
FREE_MODE_OPTIONS = (1, 0, 0)


def netbio_files() -> tuple[bytes, ...]:
    """The data files in download order."""

    return scenario_table(), room_rules()


def scenario_table() -> bytes:
    """File 0: the scenario records, disc block and descriptions."""

    table = bytearray(FILE_SIZE)
    descriptions = bytearray()
    for index in range(RECORD_COUNT):
        offset = index * 72
        if index >= len(SCENARIOS):
            struct.pack_into('<i', table, offset + 68, ABSENT)
            continue
        scenario = SCENARIOS[index]
        table[offset:offset + 3] = bytes((index + 1, DISC, index + 1))
        table[offset + 3:offset + 35] = _text(scenario.name, 32)
        table[offset + 35:offset + 52] = _counted(scenario.scenes, MAX_SCENES)
        table[offset + 52:offset + 68] = _counted(scenario.final_scenes, MAX_FINAL_SCENES)
        struct.pack_into('<i', table, offset + 68, len(descriptions))
        descriptions += _description(scenario.description) + b'\x00'
    table[DISC_BLOCK_OFFSET:DISC_BLOCK_OFFSET + 4] = bytes((*TEAM_LEVEL_THRESHOLDS, 1))
    table[DISC_TITLES_OFFSET:DISC_TITLES_OFFSET + 32] = _text(DISC_TITLE, 32)
    table[DESCRIPTIONS_OFFSET:DESCRIPTIONS_OFFSET + len(descriptions)] = descriptions
    return bytes(table)


def room_rules() -> bytes:
    """File 1: the room rules (no fixed room name or password)."""

    data = (
        bytes(rule.adjustable for rule in RULES)
        + bytes(rule.enabled for rule in RULES)
        + bytes(rule.default for rule in RULES)
        + bytes(SCENARIO_MODE_OPTIONS)
        + bytes(FREE_MODE_OPTIONS)
        + struct.pack('<h', SCENARIO_MODE_WAITING_SECONDS)
        + bytes((True, True))
    )
    return data.ljust(FILE_SIZE, b'\x00')


def _text(text: str, size: int) -> bytes:
    encoded = text.encode('ascii')
    if len(encoded) >= size:
        raise ValueError(f'{text!r} does not fit in {size} bytes')
    return encoded.ljust(size, b'\x00')


def _counted(values: tuple[int, ...], limit: int) -> bytes:
    if not 0 < len(values) <= limit:
        raise ValueError(f'{len(values)} scenes do not fit in {limit}')
    return bytes((len(values), *values)).ljust(limit + 1, b'\x00')


def _description(lines: tuple[str, ...]) -> bytes:
    return ('<BR>'.join(f'<BODY>{line}' for line in lines) + '<END>').encode('ascii')
