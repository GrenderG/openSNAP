"""Resident Evil Outbreak File#2 data files, downloaded over APP 6203/6204.

The login connection (and the data refresh after a peer announces newer data)
downloads up to two files of at most 8 KB (NA `0x001f9740`, pointers
`0x0037eea4`/`0x0037eea8`). File 0 describes the ten Areas: what the lobby
shows for each and the rules its rooms play by. `netwk.bin` keeps it in
6012-byte slots (`0x005f48d8`), so the file is exactly that long. File 1 is
only copied along on a refresh (`0x00607fc0`) and is sent small. The layout
follows, with the address of each field's reader; addresses are NA, EU runs
the same code (`0x005c2a40..`).

    10 x 484-byte Area records, then at 4840 a 20-byte trailer, then at 4860
    4 x 288-byte custom rule entries

An Area record (index = Area number - 1):

- `+0` kind, `+1`/`+2` a limited-play event and its tries: 0 here (`0x00609124`, `0x0060914c`).
- `+3` playable character mask, `+4` 16-byte extra character mask (`0x005ebfa0`).
- `+20` scenario mask ANDed with the save's unlocks, `+24` scenario mask always
  offered; bit k-1 is scenario id k, 1..11 (`0x005ebec0`). The five story
  scenarios are always offered, the elimination and showdown scenarios as the
  save unlocks them.
- `+28` title (24), `+52` subtitle (24, the room banner shows "title subtitle",
  `0x005975d0`), `+76` description (256, `<BODY>..<END>` markup).
- `+332` status: 3 lets the Area be entered (`0x00609124`), anything else
  greys it. `+333` fixed room name (16) and `+349` fixed password (14), used
  when `+363` bits 0/1 are clear (`0x005a2f24`, `0x005a3470`); here the host
  types both.
- `+364` two 60-byte rule configurations (`0x005a28a0`): 0 for titled rooms,
  1 for the quick-start categories (`0x005b3220`):

      u8 players: bit7 shown, bit6 adjustable, bits5-4 max index, bits3-2 min
         index, bits1-0 default (choices 1/2/3/4 at `0x006270a0`);
      u8 waiting time: bit7 shown, bit6 adjustable, bits5-3 count-1, bits2-0
         default (3/5/10/15/30 min); quick start: bits5-0 x 10 s, 0 = start
         when full (`0x0059e250`);
      u8 n, 10 x {u8 rule, u8 default choice, u8 shown, u8 adjustable} over
         the built-in rule table `0x00625bd0`;
      u8 m, 4 x {...} over the custom rule table at 4860.

  A hidden rule still applies its default (`0x005a3950`); a shown rule that is
  not adjustable is displayed fixed (`0x005e7848`). The rules screen reaches at
  most six rule rows, players and waiting time included. EU takes a player's
  choice only from rows with both bits 7 and 6 (`0x005c35b8`), which the
  shown-and-adjustable rows here have.

The trailer holds the auto-difficulty thresholds (`+4840`, `0x00597c50`): the
team level is the number a member's per-scenario counter reaches, the lowest
member counting, and all zeros would make "Auto" VERY HARD. Every room here
fixes its difficulty (rule 0 "difficulty control" = Fixed), so the thresholds
are not reached; File#1's values are kept for the record.

The token is the 6203 name, a digest of file 0, so every client gets the same
one and it only changes with the file. The lobby announces it on each Area
join, and of two peers with different tokens the one whose token sorts lower
re-downloads the files (`0x0059c41c`); which of two digests sorts lower is
arbitrary, so the Areas are changed with every service stopped, as the README
says, and never while players are logged in.
"""

import hashlib
import struct

from opensnap.plugins.outbreak2.directory import Area, Directory

FILE_SIZE = 6012
RECORD_SIZE = 484
RECORD_COUNT = 10
TRAILER_OFFSET = 4840
CUSTOM_RULES_OFFSET = 4860
CONFIGURATION_SIZE = 60
BUILT_IN_RULE_SLOTS = 10
FILE_1_SIZE = 16

ALL_CHARACTERS = 0xFF
# Scenario ids 1..11: bits 0..4 the story scenarios, 5..10 Elimination and Showdown.
STORY_SCENARIOS = 0x1F
ALL_SCENARIOS = 0x7FF
AREA_OPEN = 3
NAME_AND_PASSWORD_EDITABLE = 0x03
# File#1's team-level thresholds (`opensnap_app.capcom.outbreak.netbio`): the
# same scheme, a level per threshold every member's per-scenario count
# reaches.
AUTO_DIFFICULTY_THRESHOLDS = (0, 6, 31)

# Built-in rule table rows (`0x00625bd0`).
RULE_DIFFICULTY_CONTROL = 0
RULE_DIFFICULTY = 1
RULE_FRIENDLY_FIRE = 2
RULE_RANK_KIND = 13
DIFFICULTY_FIXED = 1
DIFFICULTY_NORMAL = 1
FRIENDLY_FIRE_OFF = 0

# Titled rooms: players 2/3/4 shown and adjustable, default 4
# (bit7 | bit6 | max 3 << 4 | min 1 << 2 | default 3); waiting time shown and
# adjustable with the five choices, default 5 min (bit7 | bit6 | 4 << 3 | 1).
TITLED_PLAYERS = 0x80 | 0x40 | (3 << 4) | (1 << 2) | 3
TITLED_WAITING = 0x80 | 0x40 | (4 << 3) | 1
# Quick start: 2..4 players hidden (max 3 << 4 | min 1 << 2), starting when
# full or after 3 minutes (18 x 10 s) with at least two players.
QUICK_START_PLAYERS = (3 << 4) | (1 << 2)
QUICK_START_WAITING = 18

TOKEN_PREFIX = b'OPENSNAP-'


def data_files(directory: Directory) -> tuple[bytes, ...]:
    """The data files in download order."""

    return area_table(directory), bytes(FILE_1_SIZE)


def data_token(directory: Directory) -> bytes:
    """The 6203 name: a digest of file 0, at most 31 bytes."""

    return TOKEN_PREFIX + hashlib.sha1(area_table(directory)).hexdigest()[:20].encode('ascii')


def area_table(directory: Directory) -> bytes:
    """File 0: the ten Area records, the trailer and an empty custom rule table."""

    table = bytearray(FILE_SIZE)
    for index, area in enumerate(directory.areas[:RECORD_COUNT]):
        table[index * RECORD_SIZE:(index + 1) * RECORD_SIZE] = _record(area)
    table[TRAILER_OFFSET:TRAILER_OFFSET + 3] = bytes(AUTO_DIFFICULTY_THRESHOLDS)
    return bytes(table)


def _record(area: Area) -> bytes:
    record = bytearray(RECORD_SIZE)
    record[3] = ALL_CHARACTERS
    struct.pack_into('<2L', record, 20, ALL_SCENARIOS, STORY_SCENARIOS)
    record[28:28 + 24] = _text(area.title, 24)
    record[52:52 + 24] = _text(area.subtitle, 24)
    record[76:76 + 256] = _text(area.description, 256)
    record[332] = AREA_OPEN
    record[363] = NAME_AND_PASSWORD_EDITABLE
    record[364:364 + CONFIGURATION_SIZE] = _configuration(area, titled=True)
    record[364 + CONFIGURATION_SIZE:364 + 2 * CONFIGURATION_SIZE] = _configuration(area, titled=False)
    return bytes(record)


def _configuration(area: Area, *, titled: bool) -> bytes:
    """One 60-byte rule configuration; quick-start rooms show no rows and play the Area's defaults."""

    difficulty = area.fixed_difficulty
    rules = [
        (RULE_DIFFICULTY_CONTROL, DIFFICULTY_FIXED, False, False),
        # A fixed difficulty is shown but not adjustable; otherwise the host picks it.
        (RULE_DIFFICULTY, DIFFICULTY_NORMAL if difficulty is None else difficulty, titled, titled and difficulty is None),
        (RULE_FRIENDLY_FIRE, FRIENDLY_FIRE_OFF, titled, titled),
        (RULE_RANK_KIND, area.rank_kind, False, False),
    ]
    configuration = bytearray(CONFIGURATION_SIZE)
    configuration[0] = TITLED_PLAYERS if titled else QUICK_START_PLAYERS
    configuration[1] = TITLED_WAITING if titled else QUICK_START_WAITING
    configuration[2] = len(rules)
    for index, (rule, default, shown, adjustable) in enumerate(rules):
        configuration[3 + 4 * index:7 + 4 * index] = bytes((rule, default, shown, adjustable))
    return bytes(configuration)


def _text(text: str, size: int) -> bytes:
    encoded = text.encode('utf-8')
    if len(encoded) >= size:
        raise ValueError(f'{text!r} does not fit in {size} bytes')
    return encoded.ljust(size, b'\x00')
