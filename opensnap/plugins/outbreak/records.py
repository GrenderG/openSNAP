"""Resident Evil Outbreak scenario records.

The Capcom APP service fills these boards from the 6301 result reports
(`opensnap_app.capcom.outbreak.results`); the web Notice Board ranks them
(`opensnap_web.games.outbreak`). Addresses are `SLUS_207.65` v2; v1 holds the
same tables.
"""

GAME = 'outbreak'
# The report's scenario number indexes the main ELF scenario names table
# (`0x002342b0`).
SCENARIO_NAMES = ('Outbreak', 'Below Freezing Point', 'The Hive', 'Hellfire', 'Decisions, Decisions')
# Cleared runs, scored by clear time in game frames (lowest first), one board
# per scenario and mode: the save keeps its best times per mode too (submain
# `0x0037c560` indexes them by the report's free-mode byte).
CLEAR_BOARD_PREFIX = 'clear-'
# Every run, scored by its result points (summed per player).
POINTS_BOARD_PREFIX = 'points-'
# The clear time counts game frames at 30 per second (see the results module).
CLEAR_TICKS_PER_SECOND = 30

# Characters, as the report codes them: the 8 main characters are 0..7 (names
# table `0x00235430`); an NPC is its id + 9, the player table keeping id + 1
# with an NPC flag that the report adds as + 8 (netwk `0x0057bed8`). NPC ids are
# the rows of `0x003898e0`, whose row number indexes the NPC names table
# `0x00235d20` (netwk `0x005d1be0` -> main `0x001c1e40`).
MAIN_CHARACTERS = ('KEVIN', 'MARK', 'JIM', 'GEORGE', 'DAVID', 'ALYSSA', 'YOKO', 'CINDY')
NPC_CODE_OFFSET = 9
NPC_CHARACTERS = {
    0: 'MAC', 1: 'RODRIGEZ', 2: 'CONRAD', 3: 'HUNK:B', 4: 'HUNK', 5: 'MIGUEL', 7: 'U.S.S.1', 9: 'ARNOLD',
    10: 'MATT', 11: 'BILLY', 12: 'HURSH', 16: 'PETER', 17: 'MARVIN', 18: 'FRED', 19: 'ANDY', 21: 'JEAN',
    22: 'TONNY', 24: 'KEEPER1', 25: 'KEEPER2', 26: 'AUSTIN', 27: 'CLINT', 28: 'BONE', 29: 'BOB', 31: 'NATHAN',
    32: 'SAMUEL', 34: 'WILL', 36: 'ROGER', 38: 'CARTER', 39: 'GREG', 40: 'SCHOLAR1', 41: 'SCHOLAR2', 42: 'JAKE',
    43: 'GARY', 44: 'MAN9', 46: 'MICKY', 47: 'AL', 48: 'MASKMAN', 49: 'AL:B', 50: 'BEN', 52: 'REGAN',
    53: 'REGAN:B', 54: 'MONICA', 55: 'RINDA', 56: 'RITA', 58: 'MARY', 59: 'KATE', 62: 'DANNY', 63: 'DANNY:B',
    64: 'GILL', 65: 'GILL:B', 73: 'DOCTOR4', 80: 'KURT', 81: 'KURT:B', 82: 'GARY:B', 83: 'AL:C', 86: 'DOROTHY',
    90: 'YOKO:D', 100: 'RAYMOND', 101: 'ARTHUR', 102: 'AARON', 103: 'DORIAN', 104: 'ELLIOTT', 105: 'ERIC',
    106: 'HARRY', 112: 'UBCS1', 113: 'UBCS2', 115: 'U.S.S.2', 119: 'FIREMAN', 120: 'MAN1', 121: 'MAN2',
    122: 'MAN3', 123: 'MAN4', 124: 'MAN8', 125: 'MAN6', 126: 'MAN7', 127: 'WOMAN1', 128: 'WOMAN2', 129: 'WOMAN3',
    130: 'DOCTOR1', 132: 'DOCTOR2', 134: 'DOCTOR3', 136: 'NURSE1', 138: 'NURSE2', 140: 'FRANK', 150: 'MAN5',
    107: 'Mr.RED', 108: 'Mr.BLUE', 109: 'Mr.GREEN', 110: 'Mr.GOLD', 111: 'Mr.BLACK',
}


def clear_board(scenario: int, free_mode: bool) -> str:
    return f'{CLEAR_BOARD_PREFIX}{scenario}-{int(free_mode)}'


def points_board(scenario: int) -> str:
    return f'{POINTS_BOARD_PREFIX}{scenario}'


def character_name(code: int) -> str:
    """The game's name for a reported character code ('?' for an unknown code)."""

    if code < len(MAIN_CHARACTERS):
        return MAIN_CHARACTERS[code]
    return NPC_CHARACTERS.get(code - NPC_CODE_OFFSET, '?')
