"""Auto Modellista web routes (release/Beta2 `SLUS_206.42`, Beta1 `SLUS_204.98`)."""

from flask import Blueprint, request

from opensnap.storage.interfaces import StorageBundle
from opensnap_web.common import add_routes, dump_request, html_response, page_view, text_response
from opensnap_web.games.automodellista.pages import AM_INFO_PAGE, AM_PATCH_PAGE, AM_PATCH_PAGE_NUMBERS, AM_TABOO_PAGE
from opensnap_web.games.automodellista.rankings import (
    AM_RANK_COURSE_BOARDS,
    AM_RANK_COURSE_ROWS,
    AM_RANK_EVENT_BOARD,
    AM_RANK_EVENT_ROWS,
    AutoModellistaRankings,
)
from opensnap_web.games.automodellista.rules import AM_RULE_PAGE
from opensnap_web.games.automodellista.rules_beta1 import AM_BETA1_RULE_PAGE
from opensnap_web.signup import SignupService, add_signup_routes


class AutoModellistaWebModule:
    """Release `AM-USA` pages, signup, and rankings."""

    name = 'automodellista'
    signup_prefixes = ('amweb', 'ftpublicbeta/reg')
    info_path = '/amusa/am_info.html'
    rule_path = '/amusa/am_rule.html'
    rank_path = '/amusa/am_rank.html'
    taboo_path: str | None = '/amusa/am_taboo.html'
    upload_path = '/amusa/am_up.php'
    rule_page = AM_RULE_PAGE
    # Courses `A`..`Q` and the release event board `R`.
    rank_boards = {
        **{course: AM_RANK_COURSE_ROWS for course in AM_RANK_COURSE_BOARDS},
        AM_RANK_EVENT_BOARD: AM_RANK_EVENT_ROWS,
    }

    def blueprint(self, storage: StorageBundle) -> Blueprint:
        blueprint = Blueprint(self.name, __name__)
        add_signup_routes(blueprint, SignupService(storage.accounts), self.signup_prefixes, root_aliases=True)
        add_routes(blueprint, [self.info_path], page_view(AM_INFO_PAGE))
        add_routes(blueprint, [self.rule_path], page_view(self.rule_page))
        if self.taboo_path:
            add_routes(blueprint, [self.taboo_path], page_view(AM_TABOO_PAGE))
        # Both builds' patch URL families stay served: Beta1 fetches
        # `/amusa/patchN.html` and release `/amusa/patch/2/am_patchN.html`;
        # `N` selects chunk `'1'..'5'` of the `AM-USA-GAME-PROG` patch buffer.
        add_routes(
            blueprint,
            [
                path
                for number in AM_PATCH_PAGE_NUMBERS
                for path in (f'/amusa/patch{number}.html', f'/amusa/patch/2/am_patch{number}.html')
            ],
            page_view(AM_PATCH_PAGE),
        )

        rankings = AutoModellistaRankings(
            game=self.name,
            accounts=storage.accounts,
            records=storage.records,
            rows_by_board=self.rank_boards,
        )
        add_routes(blueprint, [self.rank_path], lambda: html_response(rankings.page()))

        def upload() -> object:
            dump_request('Handled Auto Modellista ranking upload request.')
            rankings.submit(request.form.get('crs', ''), request.form.get('utl', ''))
            return text_response()

        add_routes(blueprint, [self.upload_path], upload, methods=('GET', 'POST'))
        return blueprint


class AutoModellistaBeta1WebModule(AutoModellistaWebModule):
    """Beta1 `AM-USA` pages: own paths and rule layout, no taboo page or event board."""

    name = 'automodellista_beta1'
    info_path = '/amusa/info.html'
    rule_path = '/amusa/rule.html'
    rank_path = '/amusa/rank.html'
    # `ambeta1_bin/browser.bin` does not expose `AM-USA-GAME-TABOO`.
    taboo_path = None
    upload_path = '/amusa/up.php'
    rule_page = AM_BETA1_RULE_PAGE
    rank_boards = {course: AM_RANK_COURSE_ROWS for course in AM_RANK_COURSE_BOARDS}
