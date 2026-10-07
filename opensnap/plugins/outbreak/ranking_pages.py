"""Resident Evil Outbreak ranking pages, shown in the in-game browser.

The same pages are reached two ways, each with its own links: the lobby's
Notice Board opens the web portal (`opensnap_web.games.outbreak`), and the
lobby's DATABASE menu opens `lbs://lbs/01/DATABASE.HTM` (`netwk.bin`
`0x00629a40`), which the browser fetches from the Capcom APP service.

The browser is Monster Hunter's: the same attribute table (`0x00623908`) and
entity list, so the pages use the shared browser page style, and each page
stays far below the size Monster Hunter was seen to draw in full.

Rankings come from the result reports (`opensnap.plugins.outbreak.records`):
each scenario's fastest clear per player, in scenario mode and in free mode
(the game keeps its own best times per mode), and every player's summed
result points. The reports carry no difficulty.
"""

from collections.abc import Callable
from dataclasses import dataclass
import html

from opensnap.core.browser_pages import NOTE_COLOR, data_table, document, panel
from opensnap.plugins.outbreak.records import (
    CLEAR_TICKS_PER_SECOND,
    GAME,
    POINTS_BOARD_PREFIX,
    SCENARIO_NAMES,
    character_name,
    clear_board,
)
from opensnap.storage.interfaces import RecordStore

CLEAR_RANKING_ROWS = 10
POINTS_RANKING_ROWS = 20
# A link to this name closes the browser (`netwk.bin` `0x00596fd0`).
BROWSER_CLOSE_LINK = 'AMUSA_MENU_BACK'


@dataclass(frozen=True, slots=True)
class RankingLinks:
    """Where the ranking pages live, for one way of reaching them."""

    index: str
    index_title: str
    clear: Callable[[int], str]
    points: str


class RankingPages:
    """Ranking pages built from the stored result reports."""

    def __init__(self, records: RecordStore, links: RankingLinks) -> None:
        self._records = records
        self._links = links

    def ranking_links(self) -> str:
        """The index's list of ranking pages."""

        return (
            ''.join(
                f'<a href="{self._links.clear(scenario)}">Fastest clears: {name}</a><br>\n'
                for scenario, name in enumerate(SCENARIO_NAMES)
            )
            + f'<a href="{self._links.points}">Total result points</a><br>\n'
            + f'<br>\n<font color="{NOTE_COLOR}">Rankings count the scenarios played<br>\non this server.</font><br>\n'
        )

    def clear_page(self, scenario: int) -> str | None:
        """One scenario's fastest clears, or None for an unknown scenario."""

        if not 0 <= scenario < len(SCENARIO_NAMES):
            return None
        boards = self._records.best_by_board(GAME, CLEAR_RANKING_ROWS)
        tables = [
            data_table(
                f'{SCENARIO_NAMES[scenario]}: {mode} mode',
                ('Rank', 'Player', 'Character', 'Time'),
                [
                    (
                        str(rank),
                        _name(record.player),
                        _name(character_name(int(record.details['character']))),
                        _clear_time(record.score),
                    )
                    for rank, record in enumerate(boards.get(clear_board(scenario, free_mode), []), start=1)
                ],
                empty='No clears yet',
            )
            for mode, free_mode in (('Scenario', False), ('Free', True))
        ]
        return self._page('Fastest Clears', '<br>\n'.join(tables))

    def points_page(self) -> str:
        """Every player's result points over all scenarios."""

        totals = self._records.totals(GAME, POINTS_BOARD_PREFIX, POINTS_RANKING_ROWS)
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


def _name(player: str) -> str:
    # The browser's entity table has no `&#x27;`, so quotes stay unescaped.
    return html.escape(player, quote=False)


def _clear_time(ticks: int) -> str:
    minutes, seconds = divmod(ticks // CLEAR_TICKS_PER_SECOND, 60)
    return f'{minutes}\'{seconds:02d}"'
