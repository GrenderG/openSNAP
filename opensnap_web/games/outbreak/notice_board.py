"""Resident Evil Outbreak Notice Board: the regweb portal and the scenario rankings.

The lobby's Notice Board ("View updates and national rankings ... register/
modify your user information here", `netwk.bin` `0x006316e0`) opens the
browser at `http://regweb.reo.capcom.sf.yav4.com/reweb/index.jsp` when it is
not resuming a page (`0x005de008` copies `0x00625530`; mode `0x0038cd80`
0). The registration menu opens the same URL (main `0x001f68a4`), and so does
the Monster Hunter public beta (`SLUS_291.10`), so the portal links to the
account signup as well. The rankings are the shared ranking pages
(`opensnap.plugins.outbreak.ranking_pages`). The Outbreak public beta
(`SLUS_290.89`) opens the same URL (`netwk.bin` `0x005da700`, `0x00605250`)
and gets the portal with its own rankings.
"""

from opensnap.core.browser_pages import document, panel
from opensnap_web.common import serves
from opensnap.plugins.outbreak.ranking_pages import BOARDS, BROWSER_CLOSE_LINK, RankingLinks, RankingPages
from opensnap.plugins.outbreak.scenario_rankings import ScenarioBoards
from opensnap.storage.interfaces import RecordStore

DIRECTORY = '/reweb'
INDEX_PATHS = (f'{DIRECTORY}/', f'{DIRECTORY}/index.jsp')
SIGNUP_PREFIX = 'reweb/signup'
CLEAR_RANKING_PATH = f'{DIRECTORY}/rank_clear_<int:scenario>.html'
POINTS_RANKING_PATH = f'{DIRECTORY}/rank_points.html'
# File#2's Notice Board opens this same portal (`opensnap_web.games.outbreak2`), so
# the portal links its rankings when that module is served too.
FILE2_RANKINGS_PATH = f'{DIRECTORY}/file2/index.jsp'

NOTICE_BOARD_LINKS = RankingLinks(
    index=INDEX_PATHS[1],
    index_title='Notice Board',
    clear=lambda scenario: f'{DIRECTORY}/rank_clear_{scenario}.html',
    points=POINTS_RANKING_PATH,
)


def notice_board_rankings(records: RecordStore, boards: ScenarioBoards = BOARDS) -> RankingPages:
    return RankingPages(records, NOTICE_BOARD_LINKS, boards)


def notice_board_page(rankings: RankingPages, title: str = 'Resident Evil Outbreak') -> str:
    """The portal: account signup, then the rankings."""

    file2 = f'<a href="{FILE2_RANKINGS_PATH}">Resident Evil Outbreak File#2 rankings</a><br>\n' if serves(FILE2_RANKINGS_PATH) else ''
    return document(
        panel(
            'openSNAP Notice Board',
            f'<a href="/{SIGNUP_PREFIX}/">Register / log in</a><br>\n'
            '<br>\n'
            f'{title} rankings<br>\n'
            + rankings.ranking_links()
            + file2
            + f'<br>\n<a href="{BROWSER_CLOSE_LINK}">Return to the game</a><br>\n',
        )
    )
