"""Quest records uploaded by Monster Hunter EU after a quest (APP 6307)."""

from dataclasses import dataclass
import html
import struct

from opensnap.core.sessions import Session
from opensnap.storage.interfaces import RecordStore

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

# Record page (`03/<language>/DATABASE.HTM`, NA `02/DATABASE.HTM`). The EU
# lobby Information/Record menu opens `lbs://lbs/03/%02d/DATABASE.HTM`
# (`lobby.bin` `0x005a64f8`); NA copies `lbs://lbs/02/DATABASE.HTM` into the
# same browser's URL (NA `lobby.bin` `0x005f9170`, browser fetch `0x00621310`
# clamps the read to its buffer minus the NUL). The EU browser, whose `lbs` scheme downloads the path after `lbs://lbs/`
# over APP mode 3 (`0x005d0800` -> `0x0029da30`, 6101/6102) into a 32 KB buffer
# (`0x005dc59c`) and then overwrites its last byte with a NUL (`0x005d08b0`).
# The browser parses HTML 3 (`<TABLE>`, `<TR>`, `<TH>`, `<TD>`, `<CENTER>`,
# `<FONT>`, `<HR>`, main ELF tag table near `0x0024c700`).
RECORD_PAGE_NAME = 'DATABASE.HTM'
RECORD_PAGE_NA_PATH = b'02/' + RECORD_PAGE_NAME.encode()
RECORD_PAGE_MAX_SIZE = 0x8000 - 1
RECORD_PAGE_HUNTERS = 10
RECORD_PAGE_CLEARS_PER_QUEST = 5


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
    """Keep one quest record: clear times on `clear-<quest>`, hunts on `hunts-<quest>`."""

    if record.kind == QUEST_RECORD_CLEAR:
        records.add(
            game=game,
            board=f'clear-{record.quest_id}',
            user_id=session.user_id,
            player=session.username,
            score=record.clear_seconds,
            details={'weapon_class': record.weapon_class},
        )
        return
    records.add(
        game=game,
        board=f'hunts-{record.quest_id}',
        user_id=session.user_id,
        player=session.username,
        score=sum(record.counts),
        details={
            'hunter_rank': record.hunter_rank,
            'monsters': {str(index): count for index, count in enumerate(record.counts) if count},
        },
    )


def build_record_page(records: RecordStore, game: str) -> bytes:
    """Render the shared Record page: monsters hunted and fastest clears per quest.

    Quest names and weapon classes stay numeric: their client tables are not
    decoded yet.
    """

    hunters = records.totals(game, HUNTS_BOARD_PREFIX, RECORD_PAGE_HUNTERS)
    head = [
        '<HTML><HEAD><TITLE>Hunter Records</TITLE></HEAD><BODY>',
        '<CENTER><H2>Hunter Records</H2></CENTER>',
        '<H3>Monsters hunted</H3>',
        _table(('Rank', 'Hunter', 'Monsters'), [(total.player, str(total.total)) for total in hunters]),
        '<H3>Fastest quest clears</H3>',
    ]
    tail = ['</BODY></HTML>', '']
    page = '\n'.join(head + tail).encode()
    boards = records.best_by_board(game, RECORD_PAGE_CLEARS_PER_QUEST)
    clears = sorted(
        (int(board[len(CLEAR_BOARD_PREFIX):]), rows) for board, rows in boards.items()
        if board.startswith(CLEAR_BOARD_PREFIX)
    )
    sections: list[str] = []
    for quest_id, rows in clears:
        section = f'<P>Quest {quest_id}</P>\n' + _table(
            ('Rank', 'Hunter', 'Time'), [(record.player, _clear_time(record.score)) for record in rows]
        )
        candidate = '\n'.join(head + sections + [section] + tail).encode()
        if len(candidate) > RECORD_PAGE_MAX_SIZE:
            break
        sections.append(section)
        page = candidate
    return page


def _table(header: tuple[str, ...], rows: list[tuple[str, str]]) -> str:
    lines = ['<TABLE BORDER="1">', '<TR>' + ''.join(f'<TH>{cell}</TH>' for cell in header) + '</TR>']
    lines += [
        f'<TR><TD>{rank}</TD><TD>{html.escape(name)}</TD><TD>{value}</TD></TR>'
        for rank, (name, value) in enumerate(rows, start=1)
    ]
    if not rows:
        lines.append(f'<TR><TD COLSPAN="{len(header)}">No records yet</TD></TR>')
    lines.append('</TABLE>')
    return '\n'.join(lines)


def _clear_time(seconds: int) -> str:
    minutes, seconds = divmod(seconds, 60)
    return f'{minutes}\'{seconds:02d}"'
