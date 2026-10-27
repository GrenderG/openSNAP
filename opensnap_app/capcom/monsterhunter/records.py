"""Quest records (APP 6307) and Hunter Rank Points (APP 6321) uploaded after a quest, and the Record pages."""

from collections.abc import Callable
from dataclasses import dataclass
import html
import json
import logging
from pathlib import Path
import struct

from opensnap.core.browser_pages import data_table, document, panel
from opensnap.core.online import OnlinePlayer
from opensnap.core.records import RankedRecord, Ranking, Record
from opensnap.storage.interfaces import RecordStore

_LOGGER = logging.getLogger('opensnap_app.capcom.monsterhunter')

# 6307 kinds (EU builder `0x002a3e00`). After 6306 the client sends kind 1 if
# any monster was hunted, then kind 2 if the quest clear was timed, then 6320
# (`0x002a3d50`, `0x002a4050`); the NA build has no 6307.
QUEST_RECORD_HUNTS = 1
QUEST_RECORD_CLEAR = 2
QUEST_RECORD_MONSTERS = 34
# Hunt counts are capped by the client.
QUEST_RECORD_MAX_COUNT = 9999
CLEAR_BOARD_PREFIX = 'clear-'

KILLS_BOARD_PREFIX = 'kills-'

# Hunter summary (6321 resource type 1, 20 bytes, built from the current
# hunter by NA `sub_main.bin` `0x0055f700`, EU `0x0051ee20`); little-endian:
# +0 u8 hunter +0x635, +1 u8 +0x63b, +2 u16 +0x636, +4..6 u8 +0x63c..0x63e,
# +7 u8 +0x63a, +8 u32 rare-item mask (+0x6c8), +12 u32 Hunter Rank Points
# (+0x1c), +16 u8 Hunter Rank (+0x5e3), +17..19 unused. HRP is capped at
# 9,999,999 and can drop: a failed quest or carts subtract points
# (NA `0x0055c790`).
#
# Rare-item mask: the first time the hunter obtains one of these items its
# bit is set (NA `0x0055f630`, EU `0x0051ed50`; item -> bit NA `0x0055f3e0`,
# EU `0x0051eb00`, the same table); nothing seen clears it. It is saved
# with the hunter, so a save editor can set it too; a hunter owning gear
# whose rare parts have no bit is only a hint of an edited save. Bit 31 is
# unused. Bit: item id (EU name)
#    0: 142 Dragonite Ore     11: 245 Vespoid Abdomen    22: 268 Fatalis' Horn
#    1: 143 Union Ore         12: 248 Vespoid Bladefin   23: 270 Fatalis' Shell
#    2: 144 Firestone         13: 250 Hornetaur Head     24: 271 Fatalis' Crust
#    3: 147 Novacrystal       14: 252 Hornet Bladefin    25: 272 Fatalis' Eye
#    4: 218 Rathian Plate     15: 258 Monoblos Heart     26: 273 Fatalis' Webbing
#    5: 223 Rathalos Plate    16: 259 Kirin Horn         27: 279 Guild Ticket
#    6: 230 Pale Bone         17: 261 Kirin Mane         28: 280 Silver Ticket
#    7: 233 Basarios Wing     18: 262 Lao-Shan's Claw    29: 281 Gold Ticket
#    8: 235 Gravios Carapace  19: 263 Lao-Shan's Horn    30: 285 Chrome Voucher
#    9: 237 Gravios Head      20: 265 Lao-Shan's Plate
#   10: 241 Diablos Spine     21: 267 Lao-Shan's Spine
HUNTER_SUMMARY = 1
HUNTER_SUMMARY_SIZE = 20
HUNTER_SUMMARY_HRP_OFFSET = 12
HUNTER_RANK_POINTS_MAX = 9_999_999
HRP_BOARD = 'hrp'

# Record pages. The EU lobby Information/Record menu opens
# `lbs://lbs/03/%02d/DATABASE.HTM` (`lobby.bin` `0x005a64f8`); NA copies
# `lbs://lbs/02/DATABASE.HTM` into the same browser's URL (NA `lobby.bin`
# `0x005f9170`). The browser loads every URL through one loader (NA table
# `0x00690d80`): `lbs://` URLs (scheme 5, fetch `0x00621310`) become an APP
# mode 3 download of the path after `lbs://lbs/` (NA `0x00622fd0` passes URL
# + 10; EU `0x005d0800` -> `0x0029da30`), so links to other `lbs://lbs/`
# pages are served through 6101/6102 like the Record page itself. Links
# name the page alone: the browser joins a relative link with the current
# page's URL up to its last `/` (NA `0x00630320` -> `0x00630140`, EU
# `0x005ddb20` -> `0x005dd940`; exit names are matched first, NA
# `0x0063dfa0`, EU `0x005eb970`), which keeps pages small. The
# download buffer is 32 KB, whose last byte becomes a NUL (EU `0x005dc59c`,
# `0x005d08b0`), but the EU browser stopped drawing a 16.7 KB page with 441
# table cells after its 232nd cell (byte 8960); the limit is not traced.
# Pages stay within RECORD_PAGE_SAFE_SIZE and RECORD_PAGE_SAFE_CELLS,
# the size and cell count of pages seen drawn in full. The browser parses HTML 3 and uses the shared
# browser page style (`opensnap.core.browser_pages`). Its entity table knows
# only `lt gt quot amp nbsp #034 #34 #039 #39` (NA main `0x0038a398`), so
# text is escaped without quotes (no `&#x27;`).
RECORD_PAGE_NAME = 'DATABASE.HTM'
# Current HRP: the top HRP_PAGES * HRP_PAGE_HUNTERS hunters, one table per
# page (3 cells per row, within RECORD_PAGE_SAFE_CELLS).
HRP_PAGE_CODE = 'HRP'
HRP_PAGE_HUNTERS = 25
HRP_PAGES = 4
# The EU test build's lifetime kill totals (`TEST_BUILD_RECORD_MONSTERS`),
# linked from the index once any is stored.
TEST_BUILD_KILLS_PAGE_NAME = 'TLES_KILLS.HTM'
TEST_BUILD_KILLS_PER_MONSTER = 5
RECORD_PAGE_SAFE_SIZE = 6 * 1024
RECORD_PAGE_SAFE_CELLS = 156
MONSTER_PAGE_CODE = 'MON'
RECORD_PAGE_CLEARS_PER_QUEST = 4
RECORD_PAGE_KILLS_PER_MONSTER = 3
# Each board lists the viewer's own row in this color, below the others with
# its rank when the viewer is not among them. With that row, 6 quest tables
# of 19 cells and 8 monster tables of 16 cells keep a page within
# RECORD_PAGE_SAFE_SIZE and RECORD_PAGE_SAFE_CELLS.
OWN_RECORD_COLOR = '#FFFF00'
RECORD_PAGE_QUESTS_PER_PAGE = 6
RECORD_PAGE_MONSTERS_PER_PAGE = 8
# Page numbers linked on each side of the current page (plus the first and last).
PAGE_NUMBERS_AROUND = 2
# `{"<quest number>": {"name": "...", "category": "..."}}`. The 6307 quest is
# the quest file's number (EU quest start copies quest information +0x1e into
# the uploaded word, `lobby.bin` `0x00577558`); EU uploads records, and NA
# once the openSNAP client patch is installed (`tools/monsterhunter`).
QUESTS_FILE = 'quests.json'
# Category key -> (page code, title). Quests missing from `quests.json` are
# listed under `other`.
QUEST_CATEGORIES = {
    'hunt': ('HUNT', 'Hunt quests'),
    'gathering': ('GATH', 'Gathering quests'),
    'capture': ('CAPT', 'Capture quests'),
    'special': ('SPEC', 'Special quests'),
    'event': ('EVNT', 'Event quests'),
    'other': ('OTHR', 'Other quests'),
}
# A link to this name closes the lobby browser (NA `lobby.bin` `0x0063dfe8`
# compares the HREF, then `0x0063f5b0` closes; EU has the same name), which
# returns to the lobby menu that opened the Record pages.
BROWSER_CLOSE_LINK = 'AMUSA_MENU_BACK'
_CATEGORY_BY_CODE = {code: category for category, (code, _) in QUEST_CATEGORIES.items()}
# Hunt counts: record index k counts monster k + 1 (`Quest_enemy_die`, EU
# `0x001e6fa0`, adds to `0x003aebb4 + 2 * monster`; 6307 sends
# `0x003aebb6 + 2 * k`). Names follow the quest template's MonsterID enum.
MONSTER_NAMES = {
    1: 'Rathian', 2: 'Fatalis', 3: 'Kelbi', 4: 'Mosswine', 5: 'Bullfango', 6: 'Yian Kut-Ku',
    7: 'Lao-Shan Lung', 8: 'Cephadrome', 9: 'Felyne', 11: 'Rathalos', 12: 'Aptonoth', 13: 'Genprey',
    14: 'Diablos', 15: 'Khezu', 16: 'Velociprey', 17: 'Gravios', 19: 'Vespoid', 20: 'Gypceros',
    21: 'Plesioth', 22: 'Basarios', 23: 'Melynx', 24: 'Hornetaur', 25: 'Apceros', 26: 'Monoblos',
    27: 'Velocidrome', 28: 'Gendrome', 29: 'Rock', 30: 'Ioprey', 31: 'Iodrome', 33: 'Kirin',
    34: 'Cephalos',
}
# Veggie Elder (10), Cart (18) and Poogie (32) are ignored for kill tracking:
# they can't be legally killed, so their counts are never stored or shown.
UNTRACKED_MONSTERS = {10, 18, 32}


@dataclass(frozen=True, slots=True)
class QuestRecord:
    """One decoded 6307 upload.

    Kind 1: `u8 1, u8 Hunter Rank, u16 quest, 34 x (u8 monster, u32 count)`;
    the client adds these counts to its saved hunt totals once acknowledged.
    Kind 2: `u8 2, u8 weapon class, u16 quest, u8 0, u32 clear seconds`; the
    quest timer accumulates hardware clock seconds from quest start
    (`0x001efa20`) and the weapon class comes from the equipped weapon type.
    """

    kind: int
    quest_id: int
    hunter_rank: int = 0
    counts: tuple[int, ...] = ()
    weapon_class: int = 0
    clear_seconds: int = 0


def decode_quest_record(payload: bytes) -> QuestRecord:
    """Decode one 6307 request payload."""

    if len(payload) < 4:
        raise ValueError('Quest record is truncated.')
    kind, value, quest_id = struct.unpack_from('>BBH', payload)
    if kind == QUEST_RECORD_HUNTS:
        if len(payload) != 4 + 5 * QUEST_RECORD_MONSTERS:
            raise ValueError('Hunt record has an invalid size.')
        entries = [struct.unpack_from('>BL', payload, 4 + 5 * index) for index in range(QUEST_RECORD_MONSTERS)]
        if any(monster != index or count > QUEST_RECORD_MAX_COUNT for index, (monster, count) in enumerate(entries)):
            raise ValueError('Hunt record has invalid monster entries.')
        return QuestRecord(
            kind=kind, quest_id=quest_id, hunter_rank=value, counts=tuple(count for _, count in entries)
        )
    if kind == QUEST_RECORD_CLEAR:
        if len(payload) != 9:
            raise ValueError('Clear record has an invalid size.')
        return QuestRecord(
            kind=kind, quest_id=quest_id, weapon_class=value, clear_seconds=struct.unpack_from('>L', payload, 5)[0]
        )
    raise ValueError(f'Unknown quest record kind {kind}.')


def decode_hunter_summary(block: bytes) -> int:
    """Hunter Rank Points of one decoded 6321 type 1 resource block."""

    if len(block) != HUNTER_SUMMARY_SIZE:
        raise ValueError('Hunter summary has an invalid size.')
    points = struct.unpack_from('<L', block, HUNTER_SUMMARY_HRP_OFFSET)[0]
    if points > HUNTER_RANK_POINTS_MAX:
        raise ValueError('Hunter summary has invalid Hunter Rank Points.')
    return points


def store_quest_record(records: RecordStore, game: str, player: OnlinePlayer, record: QuestRecord) -> None:
    """Keep one quest record.

    A clear time is kept on `clear-<quest>` when it is the player's fastest;
    hunted monsters add to the player's `kills-<monster>` totals.
    """

    if record.kind == QUEST_RECORD_CLEAR:
        records.submit(
            game=game,
            board=f'{CLEAR_BOARD_PREFIX}{record.quest_id}',
            ranking=Ranking.LOWEST,
            user_id=player.user_id,
            player=player.username,
            score=record.clear_seconds,
            details={'weapon_class': record.weapon_class},
        )
        return
    for index, count in enumerate(record.counts):
        if count and index + 1 not in UNTRACKED_MONSTERS:
            records.submit(
                game=game,
                board=_kills_board(index + 1),
                ranking=Ranking.TOTAL,
                user_id=player.user_id,
                player=player.username,
                score=count,
            )


# The EU test build `TLES_527.07` sends its own 6307 after 6306 (`0x002b2da0`)
# when a large-monster kill is pending (`0x0040be43`): `u8 1, u8 Hunter Rank,
# u16 quest`, then `u8 kind, u32 total` unless the kind is 0. Its quest death
# handlers (`game.bin` `0x00615f90`, `0x0068b120`) call `0x005543a0` with a
# kind from the monster type (11 -> 1, other types of the Rathian/Rathalos
# handler -> 0, 6 -> 2, 20 -> 3), which adds one to that monster's saved total
# (hunter `+1732/+1744/+1740/+1742`, capped at 9999) and keeps the new total
# for the upload. The pair loop stops at a zero kind byte, so a Rathian kill is
# reported without its pair. Hunter Rank counts the HR point thresholds
# reached (`0x005517e0`, table `0x00581560`); the quest is quest information
# `+0x1e` (`lobby.bin` `0x005bc240`), as on EU.
TEST_BUILD_RECORD_MONSTERS = {0: 1, 1: 11, 2: 6, 3: 20}
TEST_BUILD_KILLS_BOARD_PREFIX = 'tles-kills-'


@dataclass(frozen=True, slots=True)
class TestBuildQuestRecord:
    """One decoded TLES 6307 upload; `total` is None for a Rathian kill."""

    quest_id: int
    hunter_rank: int
    monster: int
    total: int | None


def decode_test_build_quest_record(payload: bytes) -> TestBuildQuestRecord:
    """Decode one TLES 6307 request payload."""

    if len(payload) not in (4, 9) or payload[0] != 1:
        raise ValueError('TLES quest record has an invalid size or flag.')
    _, hunter_rank, quest_id = struct.unpack_from('>BBH', payload)
    if len(payload) == 4:
        return TestBuildQuestRecord(quest_id, hunter_rank, TEST_BUILD_RECORD_MONSTERS[0], None)
    kind, total = struct.unpack_from('>BL', payload, 4)
    if kind not in TEST_BUILD_RECORD_MONSTERS or kind == 0 or total > QUEST_RECORD_MAX_COUNT:
        raise ValueError('TLES quest record has an invalid kill entry.')
    return TestBuildQuestRecord(quest_id, hunter_rank, TEST_BUILD_RECORD_MONSTERS[kind], total)


def store_test_build_quest_record(
    records: RecordStore, game: str, player: OnlinePlayer, record: TestBuildQuestRecord
) -> None:
    """Keep a TLES lifetime kill total on `tles-kills-<monster>`, the player's highest.

    A Rathian report carries no total, so it is not stored. These totals are
    the client's own saved counts, unlike EU's per-quest counts, so they stay
    off the other Record pages.
    """

    if record.total is None:
        return
    records.submit(
        game=game,
        board=_test_build_kills_board(record.monster),
        ranking=Ranking.HIGHEST,
        user_id=player.user_id,
        player=player.username,
        score=record.total,
        details={'quest': record.quest_id, 'hunter_rank': record.hunter_rank},
    )


@dataclass(frozen=True, slots=True)
class QuestInfo:
    """One quest as listed in `quests.json`."""

    name: str
    category: str


def load_quests(data_directory: Path) -> dict[int, QuestInfo]:
    """Read `quests.json`; quests it does not list are shown by number under `other`."""

    path = data_directory / QUESTS_FILE
    if not path.exists():
        return {}
    try:
        entries = json.loads(path.read_text(encoding='utf-8-sig')).items()
        quests = {int(quest): QuestInfo(str(entry['name']), str(entry['category'])) for quest, entry in entries}
        unknown = {quest.category for quest in quests.values()} - set(QUEST_CATEGORIES)
        if unknown:
            raise ValueError(f'unknown categories {sorted(unknown)}')
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        _LOGGER.warning('Ignoring invalid %s: %s', path, exc)
        return {}
    return quests


class RecordPages:
    """The Record pages: an index, then paged monster and quest category pages.

    Pages are named inside the directory the lobby asked for (NA `02/`, EU
    `03/<language>/`), and their relative links stay in it. Each page reads
    only the boards it shows: their top records and the viewer's own.
    """

    def __init__(self, records: RecordStore, game: str, quests: dict[int, QuestInfo]) -> None:
        self._records = records
        self._game = game
        self._quests = quests

    def render(self, name: bytes, viewer: int | None = None) -> bytes | None:
        """Return the page `name` asks for, or None when it is not a Record page.

        `viewer` is the user id of the player whose own rows are shown.
        """

        directory, _, page = name.decode('ascii', errors='replace').rpartition('/')
        if not _is_record_directory(directory):
            return None
        if page == RECORD_PAGE_NAME:
            return document(self._index()).encode()
        if page == TEST_BUILD_KILLS_PAGE_NAME:
            return document(self._test_build_kills(viewer)).encode()
        parsed = _parse_paged_name(page)
        if parsed is None:
            return None
        code, number = parsed
        if code == MONSTER_PAGE_CODE:
            content = self._monsters(number, viewer)
        elif code == HRP_PAGE_CODE:
            content = self._hunter_rank_points(number, viewer)
        else:
            content = self._category(_CATEGORY_BY_CODE[code], number, viewer)
        return None if content is None else document(content).encode()

    def _index(self) -> str:
        boards = self._records.boards(self._game)
        clears = self._ranked_quests(boards)
        lines = [
            f'<a href="{_page_name(HRP_PAGE_CODE, 1)}">Hunter Rank Points</a><br>\n',
            f'<a href="{_page_name(MONSTER_PAGE_CODE, 1)}">Most monsters hunted</a><br>\n',
        ]
        for category, (code, title) in QUEST_CATEGORIES.items():
            count = len(clears.get(category, []))
            if category == 'other' and not count:
                continue
            ranked = f'{count} quest{"s" if count != 1 else ""}'
            lines.append(f'<a href="{_page_name(code, 1)}">{title}</a> ({ranked})<br>\n')
        if self._test_build_monsters(boards):
            lines.append(f'<a href="{TEST_BUILD_KILLS_PAGE_NAME}">EU test build kill totals</a><br>\n')
        return panel(
            'Hunter Records',
            f'Your own records are shown in <font color="{OWN_RECORD_COLOR}">yellow</font>.<br>\n<br>\n'
            + ''.join(lines)
            + f'<br>\n<a href="{BROWSER_CLOSE_LINK}">Close</a><br>\n',
        )
        # Unpatched, only the EU build uploads quest records (6307); the
        # openSNAP patch adds them to NA, so this note is no longer shown:
        # f'<br>\n<font color="{NOTE_COLOR}">Unless patched, only the European release sends<br>\n'
        # 'quest and monster records, so those rankings<br>\nlist EU players only.</font><br>\n'

    def _hunter_rank_points(self, number: int, viewer: int | None) -> str | None:
        """Current HRP, highest first; hunters at the maximum rank by who reached it first."""

        top = self._records.top(self._game, HRP_BOARD, HRP_PAGES * HRP_PAGE_HUNTERS)
        own = self._own(HRP_BOARD, viewer)
        return _paged(
            'Hunter Rank Points', list(range(0, len(top), HRP_PAGE_HUNTERS)), number, 1, HRP_PAGE_CODE,
            lambda start: _board(
                'Hunters', 'HRP', top[start:start + HRP_PAGE_HUNTERS], own, str, first_rank=start + 1
            ),
            empty=_board('Hunters', 'HRP', []),
        )

    def _test_build_kills(self, viewer: int | None) -> str:
        bar = f'<a href="{RECORD_PAGE_NAME}">Index</a><br>\n'
        boards = [
            self._ranked_board(
                MONSTER_NAMES[monster], 'Kills', _test_build_kills_board(monster), TEST_BUILD_KILLS_PER_MONSTER,
                viewer, str,
            )
            for monster in self._test_build_monsters(self._records.boards(self._game))
        ] or [_board('Monsters', 'Kills', [])]
        return (
            panel(
                'EU Test Build Kill Totals',
                'Lifetime kills the EU test build saves and<br>\nreports after each quest. It reports<br>\n'
                'Rathalos, Yian Kut-Ku and Gypceros kills;<br>\nRathian kills come without a total.<br>\n' + bar,
            )
            + ''.join(f'<br>\n{board}' for board in boards)
            + f'<br>\n{bar}'
        )

    def _monsters(self, number: int, viewer: int | None) -> str | None:
        boards = set(self._records.boards(self._game))
        monsters = [monster for monster in MONSTER_NAMES if _kills_board(monster) in boards]
        return _paged(
            'Most Monsters Hunted', monsters, number, RECORD_PAGE_MONSTERS_PER_PAGE, MONSTER_PAGE_CODE,
            lambda monster: self._ranked_board(
                MONSTER_NAMES[monster], 'Kills', _kills_board(monster), RECORD_PAGE_KILLS_PER_MONSTER, viewer, str
            ),
            empty=_board('Monsters', 'Kills', []),
        )

    def _category(self, category: str, number: int, viewer: int | None) -> str | None:
        code, title = QUEST_CATEGORIES[category]
        return _paged(
            title, self._ranked_quests(self._records.boards(self._game)).get(category, []), number,
            RECORD_PAGE_QUESTS_PER_PAGE, code,
            lambda quest_id: self._ranked_board(
                self._quest_title(quest_id), 'Time', f'{CLEAR_BOARD_PREFIX}{quest_id}', RECORD_PAGE_CLEARS_PER_QUEST,
                viewer, _clear_time,
            ),
            empty=_board('Fastest clears', 'Time', []),
        )

    def _ranked_board(
        self, title: str, value: str, board: str, count: int, viewer: int | None, show: Callable[[int], str]
    ) -> str:
        return _board(title, value, self._records.top(self._game, board, count), self._own(board, viewer), show)

    def _own(self, board: str, viewer: int | None) -> RankedRecord | None:
        return None if viewer is None else self._records.entry(self._game, board, viewer)

    def _ranked_quests(self, boards: list[str]) -> dict[str, list[int]]:
        """Quests with a clear time, in quest order, by category."""

        quest_ids = sorted(
            int(board[len(CLEAR_BOARD_PREFIX):]) for board in boards if board.startswith(CLEAR_BOARD_PREFIX)
        )
        by_category: dict[str, list[int]] = {}
        for quest_id in quest_ids:
            quest = self._quests.get(quest_id)
            by_category.setdefault(quest.category if quest else 'other', []).append(quest_id)
        return by_category

    @staticmethod
    def _test_build_monsters(boards: list[str]) -> list[int]:
        return [
            monster for monster in TEST_BUILD_RECORD_MONSTERS.values() if _test_build_kills_board(monster) in boards
        ]

    def _quest_title(self, quest_id: int) -> str:
        quest = self._quests.get(quest_id)
        return f'#{quest_id} {html.escape(quest.name, quote=False)}' if quest else f'Quest {quest_id}'


def _paged[T](
    title: str, items: list[T], number: int, per_page: int, code: str,
    render: Callable[[T], str], *, empty: str,
) -> str | None:
    """Page `number` of `items`, each rendered as a board by `render`.

    A title panel with Previous / Index / Next and the page numbers, the
    page's boards, the links again (spaced with `&nbsp;`).
    """

    pages = max(1, -(-len(items) // per_page))
    if number > pages:
        return None
    navigation = [f'<a href="{RECORD_PAGE_NAME}">Index</a>']
    if number > 1:
        navigation.insert(0, f'<a href="{_page_name(code, number - 1)}">Previous</a>')
    if number < pages:
        navigation.append(f'<a href="{_page_name(code, number + 1)}">Next</a>')
    bar = ' | '.join(navigation) + '<br>\n'
    if pages > 1:
        bar += _page_numbers(code, number, pages) + '<br>\n'
    shown = [render(item) for item in items[(number - 1) * per_page:number * per_page]] or [empty]
    # Outside a table cell the browser strips the spaces that start the text
    # after a tag (NA `lobby.bin` `0x006445d0`), so `</a> |` shows `</a>|`.
    # `&nbsp;` becomes 0x07, which is not stripped and is drawn as a space
    # (`0x00642384`, `0x00646670`). The trailing `<br>` is dropped to keep the
    # largest page within RECORD_PAGE_SAFE_SIZE.
    bottom = bar.replace('> ', '>&nbsp;').removesuffix('<br>\n')
    return (
        panel(title, f'Page {number} of {pages}<br>\n{bar}')
        + ''.join(f'<br>\n{board}' for board in shown)
        + f'<br>\n{bottom}'
    )


def _page_numbers(code: str, number: int, pages: int) -> str:
    """`1 ... 4 5 <b>6</b> 7 8 ... 17`: the first, last and nearby pages, each but `number` linked."""

    shown = {1, pages, *range(max(1, number - PAGE_NUMBERS_AROUND), min(pages, number + PAGE_NUMBERS_AROUND) + 1)}
    # A gap of one page shows that page instead of `...`.
    shown |= {page + 1 for page in shown if page + 2 in shown}
    parts = []
    for previous, page in zip([0, *sorted(shown)], sorted(shown)):
        if page > previous + 1:
            parts.append('...')
        parts.append(f'<b>{page}</b>' if page == number else f'<a href="{_page_name(code, page)}">{page}</a>')
    return ' '.join(parts)


def _is_record_directory(directory: str) -> bool:
    """NA `02`, EU `03/<two-digit language>`, EU test build `03` (`lobby.bin` `lbs://lbs/03/DATABASE.HTM`)."""

    parts = directory.split('/')
    if parts in (['02'], ['03']):
        return True
    return len(parts) == 2 and parts[0] == '03' and len(parts[1]) == 2 and parts[1].isdigit()


def _page_name(code: str, number: int) -> str:
    return f'RANK_{code}_{number}.HTM'


def _parse_paged_name(page: str) -> tuple[str, int] | None:
    """`RANK_<code>_<n>.HTM` -> (code, n) for the monster or a category code."""

    if not (page.startswith('RANK_') and page.endswith('.HTM')):
        return None
    code, _, number = page[len('RANK_'):-len('.HTM')].partition('_')
    if code not in (MONSTER_PAGE_CODE, HRP_PAGE_CODE) and code not in _CATEGORY_BY_CODE:
        return None
    if not number.isdigit() or int(number) < 1:
        return None
    return code, int(number)


def _kills_board(monster: int) -> str:
    return f'{KILLS_BOARD_PREFIX}{monster:02d}'


def _test_build_kills_board(monster: int) -> str:
    return f'{TEST_BUILD_KILLS_BOARD_PREFIX}{monster:02d}'


def _board(
    title: str, value: str, records: list[Record], own: RankedRecord | None = None,
    show: Callable[[int], str] = str, *, first_rank: int = 1,
) -> str:
    """`records` ranked from `first_rank`, with `show` formatting scores.

    The viewer's `own` record is drawn in OWN_RECORD_COLOR, and added below
    the others with its rank when it is not among them.
    """

    rows = list(enumerate(records, start=first_rank))
    if own is not None and all(record.user_id != own.record.user_id for _, record in rows):
        rows.append((own.rank, own.record))
    cells = []
    for rank, record in rows:
        row = (str(rank), html.escape(record.player, quote=False), show(record.score))
        if own is not None and record.user_id == own.record.user_id:
            row = tuple(f'<font color="{OWN_RECORD_COLOR}">{cell}</font>' for cell in row)
        cells.append(row)
    return data_table(title, ('Rank', 'Hunter', value), cells, empty='No records yet')


def _clear_time(seconds: int) -> str:
    minutes, seconds = divmod(seconds, 60)
    return f'{minutes}\'{seconds:02d}"'
