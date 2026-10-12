"""The lobby DATABASE menu's pages.

DATABASE opens the browser at `lbs://lbs/04/DATABASE.HTM` (NA `netwk.bin`
`0x00643a90`) or `lbs://lbs/06/<language>/DATABASE.HTM` (EU `0x005adf1c`; the
language code is `01` English, `02` French, `03` Spanish, `04` German or `05`
Italian, table `0x006495f0`). The browser fetches `lbs://` pages
from the APP service, named by the URL past `lbs://lbs/` (`nethttp.bin`
`0x007417b0`), so the page arrives as a 6101/6102 request for
`04/DATABASE.HTM` or `06/NN/DATABASE.HTM`. Its links point at further pages of
the same directory. The pages are the shared ranking pages
(`opensnap.plugins.outbreak.scenario_rankings`) with File#2's boards.
"""

import re

from opensnap.core.browser_pages import document, panel
from opensnap.plugins.outbreak2.directory import Directory
from opensnap.plugins.outbreak2.ranking_pages import boards
from opensnap.plugins.outbreak.scenario_rankings import BROWSER_CLOSE_LINK, RankingLinks, RankingPages
from opensnap.storage.interfaces import RecordStore

URL_PREFIX = 'lbs://lbs/'
# `04` (NA) or `06/NN` (EU), then the page.
PAGE = re.compile(r'(04|06/\d\d)/(DATABASE|RANK_POINTS|RANK_CLEAR_(\d+))\.HTM')


class DatabasePages:
    """Render the DATABASE pages a 6101 asks for."""

    def __init__(self, records: RecordStore, directory: Directory) -> None:
        self._records = records
        self._boards = boards(directory)

    def render(self, name: str) -> bytes | None:
        """The page `name` names, or None when it is not a DATABASE page."""

        match = PAGE.fullmatch(name)
        if match is None:
            return None
        directory, page, scenario = match.groups()
        base = f'{URL_PREFIX}{directory}/'
        rankings = RankingPages(
            self._records,
            RankingLinks(
                index=f'{base}DATABASE.HTM',
                index_title='Database',
                clear=lambda number: f'{base}RANK_CLEAR_{number}.HTM',
                points=f'{base}RANK_POINTS.HTM',
            ),
            self._boards,
        )
        if page == 'DATABASE':
            return document(
                panel(
                    'Database',
                    'Resident Evil Outbreak File#2 rankings<br>\n'
                    + rankings.ranking_links()
                    + f'<br>\n<a href="{BROWSER_CLOSE_LINK}">Return to the game</a><br>\n',
                )
            ).encode()
        if page == 'RANK_POINTS':
            return rankings.points_page().encode()
        content = rankings.clear_page(int(scenario))
        return None if content is None else content.encode()
