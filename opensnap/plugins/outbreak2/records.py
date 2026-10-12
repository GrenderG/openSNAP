"""Resident Evil Outbreak File#2 scenario records.

The Capcom APP service fills these boards from the 6301 result reports
(`opensnap_app.capcom.outbreak2.results`); the DATABASE pages and the web
Notice Board rank them (`opensnap.plugins.outbreak.scenario_rankings`). Addresses are
`SLUS_209.84`; `SLES_533.19` holds the same tables in its language overlays
(`tasks/re_outbreak2_results.md`).
"""

GAME = 'outbreak2'
# The report's scenario byte is the scenario id - 1 (`netaq.bin` `0x008ab01b`);
# the lobby names the id through main `0x0014e240` from the table at
# `0x00219e70` (lower-case strings at `0x00259360`), whose entry 0, "training
# ground", is never offered online.
SCENARIO_NAMES = (
    'Wild Things', 'Underbelly', 'Flashback', 'Desperate Times', 'End of the Road',
    'Elimination 1', 'Elimination 2', 'Elimination 3', 'Showdown 1', 'Showdown 2', 'Showdown 3',
)
# File#2 has no free mode: the report byte File#1 used for it carries the room
# rule "rank kind" (0..7, built-in rule table row 13, `0x00626a70`), which the
# client copies from the room options (`0x006065bc`) and never reads itself.
# The server sets it per Area (`opensnap.plugins.outbreak2.directory`) through
# the APP data file (`opensnap_app.capcom.outbreak2.netbio`), so it names a
# ranking category.
# Cleared runs, scored by clear time in game frames (lowest first), one board
# per scenario and rank kind.
CLEAR_BOARD_PREFIX = 'clear-'
# Every run, scored by its result points (summed per player).
POINTS_BOARD_PREFIX = 'points-'
# `game.bin` adds one tick per game step (`0x00619a10`), 30 per second; the EU
# build runs an extra step every five PAL frames to keep that rate.
CLEAR_TICKS_PER_SECOND = 30

# Characters, as the report codes them: the 8 main characters are 0..7 (names
# `0x00218ad0`); an NPC is its id + 9 (`netwk.bin` `0x005939ac`). NPC ids are
# the rows of the submain table `0x003a3d70`, whose row indexes the NPC names
# table `0x00219810` (main `0x0014dc50`).
MAIN_CHARACTERS = ('Kevin', 'Mark', 'Jim', 'George', 'David', 'Alyssa', 'Yoko', 'Cindy')
NPC_CODE_OFFSET = 9
NPC_CHARACTERS = {
    0: 'Mac', 1: 'Rodrigez', 2: 'Conrad', 3: 'Hunk.B', 4: 'Hunk', 5: 'Miguel', 7: 'Luke', 9: 'Arnold',
    10: 'Matt', 11: 'Billy', 12: 'Hursh', 16: 'Peter', 17: 'Marvin', 18: 'Fred', 19: 'Andy', 21: 'Jean',
    22: 'Tony', 24: 'Patrick', 25: 'Lloyd', 26: 'Austin', 27: 'Clint', 28: 'Bone', 29: 'Bob', 31: 'Nathan',
    32: 'Samuel', 34: 'Will', 36: 'Roger', 38: 'Carter', 39: 'Greg', 40: 'Frost', 41: 'Frost.B', 42: 'Jake',
    43: 'Gary', 44: 'Richard', 46: 'Micky', 47: 'Al', 48: 'Axeman', 49: 'Al.B', 50: 'Ben', 52: 'Regan',
    53: 'Regan.B', 54: 'Monica', 55: 'Linda', 56: 'Rita', 58: 'Mary', 59: 'Kate', 62: 'Danny', 63: 'Danny.B',
    64: 'Gill', 65: 'Gill.B', 73: 'Keith', 80: 'Kurt', 81: 'Kurt.B', 82: 'Gary.B', 83: 'Al.C', 86: 'Dorothy',
    90: 'Yoko.Z', 100: 'Raymond', 101: 'Arthur', 102: 'Aaron', 103: 'Dorian', 104: 'Elliott', 105: 'Eric',
    106: 'Harry', 107: 'Mr.Red', 108: 'Mr.Blue', 109: 'Mr.Green', 110: 'Mr.Gold', 111: 'Mr.Black', 112: 'Karl',
    113: 'Dustin', 115: 'Derek', 116: 'Ms.White', 117: 'Ms.Peach', 118: 'Ms.Water', 119: 'Len', 120: 'Nicolas',
    121: 'Sean', 122: 'Philip', 123: 'Don', 124: 'Matthew', 125: 'Robert', 126: 'Chuck', 127: 'Ginger',
    128: 'Laura', 129: 'Amelia', 130: 'Ethan', 132: 'Howard', 134: 'Issac', 136: 'Kathy', 138: 'Elena',
    140: 'Frank', 150: 'Rodney',
}


def clear_board(scenario: int, rank_kind: int) -> str:
    return f'{CLEAR_BOARD_PREFIX}{scenario}-{rank_kind}'


def points_board(scenario: int) -> str:
    return f'{POINTS_BOARD_PREFIX}{scenario}'


def character_name(code: int) -> str:
    """The game's name for a reported character code ('?' for an unknown code)."""

    if code < len(MAIN_CHARACTERS):
        return MAIN_CHARACTERS[code]
    return NPC_CHARACTERS.get(code - NPC_CODE_OFFSET, '?')
