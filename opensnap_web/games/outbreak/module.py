"""Resident Evil Outbreak web routes (`reweb`)."""

from flask import Blueprint, abort

from opensnap.storage.interfaces import StorageBundle
from opensnap_web.common import add_routes, html_response, page_view
from opensnap_web.games.outbreak.notice_board import (
    CLEAR_RANKING_PATH,
    INDEX_PATHS,
    POINTS_RANKING_PATH,
    SIGNUP_PREFIX,
    notice_board_page,
    notice_board_rankings,
)
from opensnap_web.signup import SignupService, add_signup_routes


class OutbreakWebModule:
    """Notice Board, rankings and account signup for Resident Evil Outbreak (`SLUS_207.65` v1 and v2).

    The registration menu and the Notice Board both open
    `http://regweb.reo.capcom.sf.yav4.com/reweb/index.jsp` without a query
    string (`netwk.bin` `0x00608290`, `0x005de008`); see `notice_board`.
    """

    name = 'outbreak'

    def blueprint(self, storage: StorageBundle) -> Blueprint:
        blueprint = Blueprint(self.name, __name__)
        add_signup_routes(blueprint, SignupService(storage.accounts), (SIGNUP_PREFIX,))
        rankings = notice_board_rankings(storage.records)
        add_routes(blueprint, INDEX_PATHS, page_view(notice_board_page(rankings)))

        def clear_ranking(scenario: int) -> object:
            page = rankings.clear_page(scenario)
            if page is None:
                abort(404)
            return html_response(page)

        add_routes(blueprint, [CLEAR_RANKING_PATH], clear_ranking)
        add_routes(blueprint, [POINTS_RANKING_PATH], lambda: html_response(rankings.points_page()))
        return blueprint
