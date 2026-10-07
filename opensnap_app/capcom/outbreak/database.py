"""The lobby DATABASE menu's pages.

DATABASE opens the browser at `lbs://lbs/01/DATABASE.HTM` (`netwk.bin`
`0x00629a40`). The browser fetches `lbs://` pages from the APP service, named
by the URL past its first ten characters (`nethttp.bin` `0x00734870`), so the
page arrives as a 6101/6102 request for `01/DATABASE.HTM`. Its links point at
further `lbs://lbs/01/` pages, fetched the same way. The pages are the shared
ranking pages (`opensnap.plugins.outbreak.ranking_pages`).
"""

import re

from opensnap.core.browser_pages import document, panel
from opensnap.plugins.outbreak.ranking_pages import BROWSER_CLOSE_LINK, RankingLinks, RankingPages
from opensnap.storage.interfaces import RecordStore

URL_PREFIX = 'lbs://lbs/'
INDEX_PAGE = '01/DATABASE.HTM'
POINTS_PAGE = '01/RANK_POINTS.HTM'
CLEAR_PAGE = re.compile(r'01/RANK_CLEAR_(\d+)\.HTM')

DATABASE_LINKS = RankingLinks(
    index=URL_PREFIX + INDEX_PAGE,
    index_title='Database',
    clear=lambda scenario: f'{URL_PREFIX}01/RANK_CLEAR_{scenario}.HTM',
    points=URL_PREFIX + POINTS_PAGE,
)


class DatabasePages:
    """Render the DATABASE pages a 6101 asks for."""

    def __init__(self, records: RecordStore) -> None:
        self._rankings = RankingPages(records, DATABASE_LINKS)

    def render(self, name: str) -> bytes | None:
        """The page `name` names, or None when it is not a DATABASE page."""

        if name == INDEX_PAGE:
            return document(
                panel(
                    'Database',
                    'Resident Evil Outbreak rankings<br>\n'
                    + self._rankings.ranking_links()
                    + f'<br>\n<a href="{BROWSER_CLOSE_LINK}">Return to the game</a><br>\n',
                )
            ).encode()
        if name == POINTS_PAGE:
            return self._rankings.points_page().encode()
        if match := CLEAR_PAGE.fullmatch(name):
            page = self._rankings.clear_page(int(match.group(1)))
            return None if page is None else page.encode()
        return None
