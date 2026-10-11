"""Scenario ranking pages for the Resident Evil Outbreak games, shown in the in-game browser.

Both Outbreak games upload a result report after each scenario (APP 6301,
`opensnap_app.capcom.scenario_report`) and reach the rankings two ways, each
with its own links: the lobby's Notice Board opens the web portal and the
lobby's DATABASE menu opens an `lbs://` page the browser fetches from the
Capcom APP service. The browser is Monster Hunter's (File#1 attribute table
`0x00623908`, File#2 NA `0x0063de38`), so the pages use the shared browser page
style and stay far below the size Monster Hunter was seen to draw in full.

Rankings: each scenario's fastest clear per player, split the way each game's
report allows (File#1 by mode, File#2 by the rank kind of the Area), and every
player's summed result points. Each game describes its boards with
`ScenarioBoards`.
"""

from collections.abc import Callable
from dataclasses import dataclass
import html

from opensnap.core.browser_pages import NOTE_COLOR, data_table, document, panel
from opensnap.storage.interfaces import RecordStore

CLEAR_RANKING_ROWS = 10
POINTS_RANKING_ROWS = 20
# A link to this name closes the browser (File#1 `netwk.bin` `0x00596fd0`, File#2 NA `0x005b0620`).
BROWSER_CLOSE_LINK = 'AMUSA_MENU_BACK'


@dataclass(frozen=True, slots=True)
class ScenarioBoards:
    """How one game keys its records: the `game` of the record store and its board names."""

    game: str
    scenario_names: tuple[str, ...]
    # One scenario's clear boards as `(table title, board name)`.
    clear_boards: Callable[[int], tuple[tuple[str, str], ...]]
    points_board_prefix: str
    character_name: Callable[[int], str]
    clear_ticks_per_second: int
    # Leave out clear boards without records (keeping the first one when all are empty).
    hide_empty_boards: bool = False


@dataclass(frozen=True, slots=True)
class RankingLinks:
    """Where the ranking pages live, for one way of reaching them."""

    index: str
    index_title: str
    clear: Callable[[int], str]
    points: str


class RankingPages:
    """Ranking pages built from the stored result reports."""

    def __init__(self, records: RecordStore, links: RankingLinks, boards: ScenarioBoards) -> None:
        self._records = records
        self._links = links
        self._boards = boards

    def ranking_links(self) -> str:
        """The index's list of ranking pages."""

        return (
            ''.join(
                f'<a href="{self._links.clear(scenario)}">Fastest clears: {name}</a><br>\n'
                for scenario, name in enumerate(self._boards.scenario_names)
            )
            + f'<a href="{self._links.points}">Total result points</a><br>\n'
            + f'<br>\n<font color="{NOTE_COLOR}">Rankings count the scenarios played<br>\non this server.</font><br>\n'
        )

    def clear_page(self, scenario: int) -> str | None:
        """One scenario's fastest clears, or None for an unknown scenario."""

        names = self._boards.scenario_names
        if not 0 <= scenario < len(names):
            return None
        boards = self._records.best_by_board(self._boards.game, CLEAR_RANKING_ROWS)
        clear_boards = list(self._boards.clear_boards(scenario))
        if self._boards.hide_empty_boards:
            clear_boards = [(title, board) for title, board in clear_boards if boards.get(board)] or clear_boards[:1]
        tables = [
            data_table(
                title,
                ('Rank', 'Player', 'Character', 'Time'),
                [
                    (
                        str(rank),
                        _name(record.player),
                        _name(self._boards.character_name(int(record.details['character']))),
                        self._clear_time(record.score),
                    )
                    for rank, record in enumerate(boards.get(board, []), start=1)
                ],
                empty='No clears yet',
            )
            for title, board in clear_boards
        ]
        return self._page('Fastest Clears', '<br>\n'.join(tables))

    def points_page(self) -> str:
        """Every player's result points over all scenarios."""

        totals = self._records.totals(self._boards.game, self._boards.points_board_prefix, POINTS_RANKING_ROWS)
        table = data_table(
            'All scenarios',
            ('Rank', 'Player', 'Points'),
            [(str(rank), _name(total.player), str(total.total)) for rank, total in enumerate(totals, start=1)],
            empty='No results yet',
        )
        return self._page('Total Result Points', table)

    def _page(self, title: str, table: str) -> str:
        navigation = (
            f'<a href="{self._links.index}">{self._links.index_title}</a> | '
            f'<a href="{BROWSER_CLOSE_LINK}">Return to the game</a><br>\n'
        )
        return document(panel(title, navigation) + f'<br>\n{table}<br>\n' + navigation)

    def _clear_time(self, ticks: int) -> str:
        minutes, seconds = divmod(ticks // self._boards.clear_ticks_per_second, 60)
        return f'{minutes}\'{seconds:02d}"'


def _name(player: str) -> str:
    # The browser's entity table has no `&#x27;`, so quotes stay unescaped.
    return html.escape(player, quote=False)
