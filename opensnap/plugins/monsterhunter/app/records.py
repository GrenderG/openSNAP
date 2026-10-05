"""Quest records uploaded by Monster Hunter EU after a quest (APP 6307)."""

from dataclasses import dataclass
import html
import json
import logging
from pathlib import Path
import struct

from opensnap.core.browser_pages import NOTE_COLOR, data_table, document, panel
from opensnap.core.sessions import Session
from opensnap.storage.interfaces import RecordStore

_LOGGER = logging.getLogger('opensnap.plugins.monsterhunter.app')

# 6307 kinds (EU builder `0x002a3e00`). After 6306 the client sends kind 1 if
# any monster was hunted, then kind 2 if the quest clear was timed, then 6320
# (`0x002a3d50`, `0x002a4050`); the NA build has no 6307.
QUEST_RECORD_HUNTS = 1
QUEST_RECORD_CLEAR = 2
QUEST_RECORD_MONSTERS = 34
# Hunt counts are capped by the client.
QUEST_RECORD_MAX_COUNT = 9999
HUNTS_BOARD_PREFIX = 'hunts-'
CLEAR_BOARD_PREFIX = 'clear-'

KILLS_BOARD_PREFIX = 'kills-'

# Record pages. The EU lobby Information/Record menu opens
# `lbs://lbs/03/%02d/DATABASE.HTM` (`lobby.bin` `0x005a64f8`); NA copies
# `lbs://lbs/02/DATABASE.HTM` into the same browser's URL (NA `lobby.bin`
# `0x005f9170`). The browser loads every URL through one loader (NA table
# `0x00690d80`): `lbs://` URLs (scheme 5, fetch `0x00621310`) become an APP
# mode 3 download of the path after `lbs://lbs/` (NA `0x00622fd0` passes URL
# + 10; EU `0x005d0800` -> `0x0029da30`), so links to other `lbs://lbs/`
# pages are served through 6101/6102 like the Record page itself. The
# download buffer is 32 KB, whose last byte becomes a NUL (EU `0x005dc59c`,
# `0x005d08b0`), but the EU browser stopped drawing a 16.7 KB page with 441
# table cells after its 232nd cell (byte 8960); the limit is not traced.
# Pages stay within RECORD_PAGE_SAFE_SIZE and RECORD_PAGE_SAFE_CELLS,
# the size and cell count of pages seen drawn in full. The browser parses HTML 3 and uses the shared
# browser page style (`opensnap.core.browser_pages`). Its entity table knows
# only `lt gt quot amp nbsp #034 #34 #039 #39` (NA main `0x0038a398`), so
# text is escaped without quotes (no `&#x27;`).
RECORD_PAGE_NAME = 'DATABASE.HTM'
RECORD_PAGE_SAFE_SIZE = 6 * 1024
RECORD_PAGE_SAFE_CELLS = 156
MONSTER_PAGE_CODE = 'MON'
RECORD_PAGE_CLEARS_PER_QUEST = 4
RECORD_PAGE_KILLS_PER_MONSTER = 3
RECORD_PAGE_QUESTS_PER_PAGE = 8
# 10 tables of 13 cells: 132 cells, within RECORD_PAGE_SAFE_CELLS.
RECORD_PAGE_MONSTERS_PER_PAGE = 10
# `{"<quest number>": {"name": "...", "category": "..."}}`. The 6307 quest is
# the quest file's number (EU quest start copies quest information +0x1e into
# the uploaded word, `lobby.bin` `0x00577558`); EU is the only build that
# uploads records.
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


def store_quest_record(records: RecordStore, game: str, session: Session, record: QuestRecord) -> None:
    """Keep one quest record.

    Clear times go on `clear-<quest>`. Hunts go on `hunts-<quest>` (the whole
    upload) and on `kills-<monster>` (one row per hunted monster).
    """

    if record.kind == QUEST_RECORD_CLEAR:
        records.add(
            game=game,
            board=f'{CLEAR_BOARD_PREFIX}{record.quest_id}',
            user_id=session.user_id,
            player=session.username,
            score=record.clear_seconds,
            details={'weapon_class': record.weapon_class},
        )
        return
    records.add(
        game=game,
        board=f'{HUNTS_BOARD_PREFIX}{record.quest_id}',
        user_id=session.user_id,
        player=session.username,
        score=sum(record.counts),
        details={
            'hunter_rank': record.hunter_rank,
            'monsters': {str(index): count for index, count in enumerate(record.counts) if count},
        },
    )
    for index, count in enumerate(record.counts):
        if count and index + 1 not in UNTRACKED_MONSTERS:
            records.add(
                game=game,
                board=_kills_board(index + 1),
                user_id=session.user_id,
                player=session.username,
                score=count,
                details={'quest': record.quest_id},
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
    `03/<language>/`), and their links point back into it.
    """

    def __init__(self, records: RecordStore, game: str, quests: dict[int, QuestInfo]) -> None:
        self._records = records
        self._game = game
        self._quests = quests

    def render(self, name: bytes) -> bytes | None:
        """Return the page `name` asks for, or None when it is not a Record page."""

        directory, _, page = name.decode('ascii', errors='replace').rpartition('/')
        if not _is_record_directory(directory):
            return None
        links = _Links(directory)
        if page == RECORD_PAGE_NAME:
            return document(self._index(links)).encode()
        parsed = _parse_paged_name(page)
        if parsed is None:
            return None
        code, number = parsed
        if code == MONSTER_PAGE_CODE:
            content = self._monsters(number, links)
        else:
            content = self._category(_CATEGORY_BY_CODE[code], number, links)
        return None if content is None else document(content).encode()

    def _index(self, links: '_Links') -> str:
        clears = self._clears_by_category()
        lines = [f'<a href="{links.page(MONSTER_PAGE_CODE, 1)}">Most monsters hunted</a><br>\n']
        for category, (code, title) in QUEST_CATEGORIES.items():
            count = len(clears.get(category, []))
            if category == 'other' and not count:
                continue
            ranked = f'{count} quest{"s" if count != 1 else ""}'
            lines.append(f'<a href="{links.page(code, 1)}">{title}</a> ({ranked})<br>\n')
        return panel(
            'Hunter Records',
            'Fastest clears of each quest and the hunters<br>\nwith the most kills of each monster.<br>\n<br>\n'
            + ''.join(lines)
            + f'<br>\n<a href="{BROWSER_CLOSE_LINK}">Close</a><br>\n',
        ) + (
            # Only the EU build uploads quest records (6307); NA has no such command.
            f'<br>\n<font color="{NOTE_COLOR}">Only the European release sends quest records,<br>\n'
            'so these rankings list EU players only.</font><br>\n'
        )

    def _monsters(self, number: int, links: '_Links') -> str | None:
        boards = []
        for monster, name in MONSTER_NAMES.items():
            totals = self._records.totals(self._game, _kills_board(monster), RECORD_PAGE_KILLS_PER_MONSTER)
            if totals:
                boards.append(_board(name, 'Kills', [(total.player, str(total.total)) for total in totals]))
        return _paged(
            'Most Monsters Hunted', boards, number, RECORD_PAGE_MONSTERS_PER_PAGE, MONSTER_PAGE_CODE, links,
            empty=_board('Monsters', 'Kills', []),
        )

    def _category(self, category: str, number: int, links: '_Links') -> str | None:
        boards = [
            _board(self._quest_title(quest_id), 'Time', [(record.player, _clear_time(record.score)) for record in rows])
            for quest_id, rows in self._clears_by_category().get(category, [])
        ]
        code, title = QUEST_CATEGORIES[category]
        return _paged(
            title, boards, number, RECORD_PAGE_QUESTS_PER_PAGE, code, links,
            empty=_board('Fastest clears', 'Time', []),
        )

    def _clears_by_category(self) -> dict[str, list[tuple[int, list]]]:
        boards = self._records.best_by_board(self._game, RECORD_PAGE_CLEARS_PER_QUEST)
        by_category: dict[str, list[tuple[int, list]]] = {}
        for board, rows in boards.items():
            if not board.startswith(CLEAR_BOARD_PREFIX):
                continue
            quest_id = int(board[len(CLEAR_BOARD_PREFIX):])
            quest = self._quests.get(quest_id)
            by_category.setdefault(quest.category if quest else 'other', []).append((quest_id, rows))
        for quests in by_category.values():
            quests.sort(key=lambda item: item[0])
        return by_category

    def _quest_title(self, quest_id: int) -> str:
        quest = self._quests.get(quest_id)
        return f'#{quest_id} {html.escape(quest.name, quote=False)}' if quest else f'Quest {quest_id}'


@dataclass(frozen=True, slots=True)
class _Links:
    directory: str

    @property
    def index(self) -> str:
        return self._url(RECORD_PAGE_NAME)

    def page(self, code: str, number: int) -> str:
        return self._url(f'RANK_{code}_{number}.HTM')

    def _url(self, page: str) -> str:
        return f'lbs://lbs/{self.directory}/{page}'


def _paged(
    title: str, boards: list[str], number: int, per_page: int, code: str, links: _Links, *, empty: str
) -> str | None:
    """Page `number` of `boards`: a title panel with Previous / Index / Next, the boards, the links again."""

    pages = max(1, -(-len(boards) // per_page))
    if number > pages:
        return None
    navigation = [f'<a href="{links.index}">Index</a>']
    if number > 1:
        navigation.insert(0, f'<a href="{links.page(code, number - 1)}">Previous</a>')
    if number < pages:
        navigation.append(f'<a href="{links.page(code, number + 1)}">Next</a>')
    bar = ' | '.join(navigation) + '<br>\n'
    shown = boards[(number - 1) * per_page:number * per_page] or [empty]
    return (
        panel(title, f'Page {number} of {pages}<br>\n{bar}')
        + ''.join(f'<br>\n{board}' for board in shown)
        + f'<br>\n{bar}'
    )


def _is_record_directory(directory: str) -> bool:
    """NA `02`, EU `03/<two-digit language>`."""

    parts = directory.split('/')
    return parts == ['02'] or (len(parts) == 2 and parts[0] == '03' and len(parts[1]) == 2 and parts[1].isdigit())


def _parse_paged_name(page: str) -> tuple[str, int] | None:
    """`RANK_<code>_<n>.HTM` -> (code, n) for the monster or a category code."""

    if not (page.startswith('RANK_') and page.endswith('.HTM')):
        return None
    code, _, number = page[len('RANK_'):-len('.HTM')].partition('_')
    if code != MONSTER_PAGE_CODE and code not in _CATEGORY_BY_CODE:
        return None
    if not number.isdigit() or int(number) < 1:
        return None
    return code, int(number)


def _kills_board(monster: int) -> str:
    return f'{KILLS_BOARD_PREFIX}{monster:02d}'


def _board(title: str, value: str, rows: list[tuple[str, str]]) -> str:
    return data_table(
        title,
        ('Rank', 'Hunter', value),
        [(str(rank), html.escape(name, quote=False), score) for rank, (name, score) in enumerate(rows, start=1)],
        empty='No records yet',
    )


def _clear_time(seconds: int) -> str:
    minutes, seconds = divmod(seconds, 60)
    return f'{minutes}\'{seconds:02d}"'
