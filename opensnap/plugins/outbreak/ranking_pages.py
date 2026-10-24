"""Resident Evil Outbreak File#1 ranking pages (`opensnap.plugins.outbreak.scenario_rankings` with File#1's boards).

The lobby's DATABASE menu opens `lbs://lbs/01/DATABASE.HTM` (`netwk.bin`
`0x00629a40`), fetched from the Capcom APP service, and the Notice Board opens
the web portal (`opensnap_web.games.outbreak`); both show these pages.
"""

from dataclasses import replace

from opensnap.plugins.outbreak.records import (
    CLEAR_TICKS_PER_SECOND,
    GAME,
    POINTS_BOARD,
    PUBLIC_BETA_GAME,
    PUBLIC_BETA_SCENARIO_NAMES,
    SCENARIO_NAMES,
    character_name,
    clear_board,
)
from opensnap.plugins.outbreak.scenario_rankings import BROWSER_CLOSE_LINK, RankingLinks, ScenarioBoards
from opensnap.plugins.outbreak.scenario_rankings import RankingPages as _RankingPages
from opensnap.storage.interfaces import RecordStore

__all__ = ['BOARDS', 'BROWSER_CLOSE_LINK', 'PUBLIC_BETA_BOARDS', 'RankingLinks', 'RankingPages']


def _clear_boards(scenario: int) -> tuple[tuple[str, str], ...]:
    """Scenario mode and free mode, the two best-time sets the game keeps."""

    name = SCENARIO_NAMES[scenario]
    return (f'{name}: Scenario mode', clear_board(scenario, False)), (f'{name}: Free mode', clear_board(scenario, True))


BOARDS = ScenarioBoards(
    game=GAME,
    scenario_names=SCENARIO_NAMES,
    clear_boards=_clear_boards,
    points_board=POINTS_BOARD,
    character_name=character_name,
    clear_ticks_per_second=CLEAR_TICKS_PER_SECOND,
)
# The public beta's own records, the same boards for its two scenarios.
PUBLIC_BETA_BOARDS = replace(BOARDS, game=PUBLIC_BETA_GAME, scenario_names=PUBLIC_BETA_SCENARIO_NAMES)


class RankingPages(_RankingPages):
    """File#1's ranking pages (the release's boards unless `boards` says otherwise)."""

    def __init__(self, records: RecordStore, links: RankingLinks, boards: ScenarioBoards = BOARDS) -> None:
        super().__init__(records, links, boards)
