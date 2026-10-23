"""Resident Evil Outbreak File#2 web routes (`reweb/file2`, and the `reweb` portal when served alone)."""

from flask import Blueprint, abort

from opensnap.storage.interfaces import StorageBundle
from opensnap_web.common import add_routes, html_response, page_view
from opensnap_web.games.outbreak2.notice_board import (
    CLEAR_RANKING_PATH,
    INDEX_PATHS,
    POINTS_RANKING_PATH,
    PORTAL_INDEX_PATHS,
    SIGNUP_PREFIX,
    notice_board_page,
    notice_board_rankings,
    rankings_page,
)
from opensnap_web.signup import SignupService, add_signup_routes


class Outbreak2WebModule:
    """Rankings and account signup for Resident Evil Outbreak File#2 (`SLUS_209.84`, `SLES_533.19`).

    The signup shares File#1's `reweb/signup` directory (one account works in
    both games: File#2 NA reads and writes File#1's NET save
    `BASLUS-20765NET`).
    """

    name = 'outbreak2'

    def blueprint(self, storage: StorageBundle) -> Blueprint:
        blueprint = Blueprint(self.name, __name__)
        add_signup_routes(blueprint, SignupService(storage.accounts), (SIGNUP_PREFIX,))
        rankings = notice_board_rankings(storage.records)
        add_routes(blueprint, PORTAL_INDEX_PATHS, page_view(notice_board_page(rankings)))
        add_routes(blueprint, INDEX_PATHS, page_view(rankings_page(rankings)))

        def clear_ranking(scenario: int) -> object:
            page = rankings.clear_page(scenario)
            if page is None:
                abort(404)
            return html_response(page)

        add_routes(blueprint, [CLEAR_RANKING_PATH], clear_ranking)
        add_routes(blueprint, [POINTS_RANKING_PATH], lambda: html_response(rankings.points_page()))
        return blueprint
