"""Resident Evil Outbreak File#2 on the `reweb` portal: its ranking pages and its own Notice Board.

File#2's Notice Board opens the same hard-coded
`http://regweb.reo.capcom.sf.yav4.com/reweb/index.jsp` as File#1 (NA
`netwk.bin` `0x0063f960`, EU `0x0064a110`), so with every game served (the
`generic` web plugin) File#1's portal answers it and links here; a web
service serving File#2 alone shows this module's portal there. Its
registration menu arrives at the signup page the APP 6110 reply names
(`opensnap_app.capcom.outbreak2.service`). The rankings are the shared ranking
pages (`opensnap.plugins.outbreak.scenario_rankings`) with File#2's boards.
"""

from opensnap.core.browser_pages import document, panel
from opensnap.plugins.outbreak2.directory import read_directory
from opensnap.plugins.outbreak2.ranking_pages import boards
from opensnap.plugins.outbreak.scenario_rankings import BROWSER_CLOSE_LINK, RankingLinks, RankingPages
from opensnap.storage.interfaces import RecordStore

PORTAL_DIRECTORY = '/reweb'
DIRECTORY = f'{PORTAL_DIRECTORY}/file2'
PORTAL_INDEX_PATHS = (f'{PORTAL_DIRECTORY}/', f'{PORTAL_DIRECTORY}/index.jsp')
INDEX_PATHS = (f'{DIRECTORY}/', f'{DIRECTORY}/index.jsp')
SIGNUP_PREFIX = 'reweb/signup'
CLEAR_RANKING_PATH = f'{DIRECTORY}/rank_clear_<int:scenario>.html'
POINTS_RANKING_PATH = f'{DIRECTORY}/rank_points.html'

RANKING_LINKS = RankingLinks(
    index=INDEX_PATHS[1],
    index_title='File#2 rankings',
    clear=lambda scenario: f'{DIRECTORY}/rank_clear_{scenario}.html',
    points=POINTS_RANKING_PATH,
)


def notice_board_rankings(records: RecordStore) -> RankingPages:
    return RankingPages(records, RANKING_LINKS, boards(read_directory()))


def rankings_page(rankings: RankingPages) -> str:
    """The File#2 ranking index, linked from the portal."""

    return document(
        panel(
            'Resident Evil Outbreak File#2 rankings',
            rankings.ranking_links()
            + f'<br>\n<a href="{PORTAL_INDEX_PATHS[1]}">Notice Board</a> | '
            f'<a href="{BROWSER_CLOSE_LINK}">Return to the game</a><br>\n',
        )
    )


def notice_board_page(rankings: RankingPages) -> str:
    """The portal when only File#2 is served: account signup, then the rankings."""

    return document(
        panel(
            'openSNAP Notice Board',
            f'<a href="/{SIGNUP_PREFIX}/">Register / log in</a><br>\n'
            '<br>\n'
            'Resident Evil Outbreak File#2 rankings<br>\n'
            + rankings.ranking_links()
            + f'<br>\n<a href="{BROWSER_CLOSE_LINK}">Return to the game</a><br>\n',
        )
    )
