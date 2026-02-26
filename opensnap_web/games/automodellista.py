"""Auto Modellista web routes."""

import re
from collections.abc import Callable

from flask import Flask, Response, request

from opensnap_web.config import WebServerConfig
from opensnap_web.games.base import WebRouteTools
from opensnap_web.signup import SignupResult, SqliteSignupService

AM_INFO_PAGE = """<html><head>
<!--AM-USA-INFORMATION-->
</head>
<!--
<CSV>
"INFO_TAG = aaaaaaaa",
"INFO_MSG = <BODY>Test Message<END>",
</CSV>
-->
</html>
"""

AM_RULE_PAGE = """<html><head>
<!--AM-USA-GAME-RULE-->
</head>
<!-- 0 Mountain -->
<!-- 1 City -->
<!-- 2 Circuit -->
<!-- 3 Event -->
<!-- 4 Clubmeeting -->
<!-- 5 unknown -->
<!--
<CSV>
"00000a00000800000100000000000000000000240000000000000000",
"00000a00000800000100000000000000000000240000000000000000",
"00000a00000800000100000000000000000000240000000000000000",
"00000a00000800000100000000000000000000440000000000000100",
"00000000000000000000000000000000000000110000000000000000",
"00000a00000800000100000000000000000000220000000000000000",
"00"
</CSV>
-->
</html>
"""

AM_RANK_PAGE = '<html><body>am_rank</body></html>\n'
AM_TABOO_PAGE = '<html><body>am_taboo</body></html>\n'
MAX_USERNAME_LENGTH = 10
MAX_PASSWORD_LENGTH = 8
USERNAME_PATTERN = re.compile(r'^[A-Za-z0-9_.-]{1,10}$')
SIGNUP_INDEX_PAGE = (
    '<html>\n'
    '<body>\n'
    'openSNAP signup service<br>\n'
    '<br>\n'
    'Choose the username to save on your memory card.<br>\n'
    '<br>\n'
    '<form action="create_id.html" method="post">\n'
    'Username: '
    f'<input type="text" name="username" size="{MAX_USERNAME_LENGTH}" maxlength="{MAX_USERNAME_LENGTH}">\n'
    '<br>\n'
    'Password: '
    f'<input type="password" name="password" size="{MAX_PASSWORD_LENGTH}" maxlength="{MAX_PASSWORD_LENGTH}">\n'
    '<br>\n'
    '<input type="submit" value="Create/Login ID">\n'
    '</form>\n'
    '</body>\n'
    '</html>\n'
)


def register_signup_routes(
    app: Flask,
    *,
    tools: WebRouteTools,
    signup_service: SqliteSignupService,
    route_prefixes: tuple[str, ...],
    include_root_aliases: bool,
) -> None:
    """Register SNAP signup/create-id routes for one or more path prefixes."""

    normalized_prefixes = tuple(prefix.strip('/') for prefix in route_prefixes if prefix.strip('/'))
    if not normalized_prefixes:
        return

    if include_root_aliases:
        app.add_url_rule(
            '/',
            endpoint='signup_root_index',
            methods=['GET'],
            view_func=_make_signup_index_view(tools),
        )
        app.add_url_rule(
            '/login.php',
            endpoint='signup_login_index',
            methods=['GET'],
            view_func=_make_signup_index_view(tools),
        )

    for prefix in normalized_prefixes:
        app.add_url_rule(
            f'/{prefix}/index.jsp',
            endpoint=f'signup_{prefix}_index',
            methods=['GET'],
            view_func=_make_signup_index_view(tools),
        )
        app.add_url_rule(
            f'/{prefix}/create_id.html',
            endpoint=f'signup_{prefix}_create_id_query',
            methods=['GET', 'POST'],
            view_func=_make_signup_query_view(signup_service),
        )
        app.add_url_rule(
            f'/{prefix}/create_id_<username>.html',
            endpoint=f'signup_{prefix}_create_id_dynamic',
            methods=['GET'],
            view_func=_make_signup_dynamic_view(signup_service),
        )


def _make_signup_index_view(tools: WebRouteTools) -> Callable[[], Response]:
    """Build one index handler for legacy signup pages."""

    def _signup_index() -> Response:
        return tools.html_response(SIGNUP_INDEX_PAGE)

    return _signup_index


def _make_signup_query_view(signup_service: SqliteSignupService) -> Callable[[], Response]:
    """Build query/create-id handler using username from request values."""

    def _signup_query() -> Response:
        username = (request.values.get('username') or '').strip()
        password = (request.values.get('password') or '').strip()
        return _build_signup_response(
            username=username,
            password=password,
            signup_service=signup_service,
        )

    return _signup_query


def _make_signup_dynamic_view(signup_service: SqliteSignupService) -> Callable[[str], Response]:
    """Build dynamic create-id handler using username from route path."""

    def _signup_dynamic(username: str) -> Response:
        password = (request.values.get('password') or '').strip()
        return _build_signup_response(
            username=username.strip(),
            password=password,
            signup_service=signup_service,
        )

    return _signup_dynamic


class AutoModellistaWebModule:
    """Web endpoints used by Auto Modellista clients."""

    name = 'automodellista'

    def register_routes(self, app: Flask, config: WebServerConfig, tools: WebRouteTools) -> None:
        """Register Auto Modellista-specific web endpoints."""

        del config
        signup_service = SqliteSignupService()
        register_signup_routes(
            app,
            tools=tools,
            signup_service=signup_service,
            route_prefixes=('amweb',),
            include_root_aliases=True,
        )

        @app.get('/amusa/am_info.html')
        def amusa_info() -> Response:
            return tools.html_response(AM_INFO_PAGE)

        @app.get('/amusa/am_rule.html')
        def amusa_rule() -> Response:
            return tools.html_response(AM_RULE_PAGE)

        @app.get('/amusa/am_rank.html')
        def amusa_rank() -> Response:
            return tools.html_response(AM_RANK_PAGE)

        @app.get('/amusa/am_taboo.html')
        def amusa_taboo() -> Response:
            return tools.html_response(AM_TABOO_PAGE)

        @app.route('/amusa/am_up.php', methods=['GET', 'POST'])
        def amusa_upload() -> Response:
            tools.dump_request('Handled /amusa/am_up.php request.')
            return Response('', mimetype='text/plain')


def _build_signup_response(
    *,
    username: str,
    password: str,
    signup_service: SqliteSignupService,
) -> Response:
    """Build PS2 signup response payload for a selected username."""

    if not _is_valid_username(username):
        return _error_response('Invalid username.')
    if not _is_valid_password(password):
        return _error_response('Invalid password.')

    result = signup_service.create_or_login(username=username, password=password)
    if not result.ok:
        return _error_response(result.error_message)

    payload = _build_signup_payload(result)
    return Response(payload, mimetype='text/html')


def _is_valid_username(username: str) -> bool:
    """Validate signup username format and length."""

    if len(username.encode('utf-8')) > MAX_USERNAME_LENGTH:
        return False
    return USERNAME_PATTERN.fullmatch(username) is not None


def _is_valid_password(password: str) -> bool:
    """Validate password format and length."""

    if not password:
        return False
    if len(password.encode('utf-8')) > MAX_PASSWORD_LENGTH:
        return False
    return True


def _build_signup_payload(result: SignupResult) -> str:
    """Build successful COMP-SIGNUP payload."""

    return (
        '<html>\n'
        '<body>\n'
        'Profile successfully retrieved.<br>\n'
        'Press the Select button and then "End Browser" to save it to the memory card.\n'
        '</body>\n'
        '</html>\n'
        '<!--COMP-SIGNUP-->\n'
        f'<!--INPUT-IDS-->{result.username}\n'
    )


def _error_response(message: str) -> Response:
    """Build generic HTML error response."""

    page = (
        '<html>\n'
        '<body>\n'
        '<h3>Login error</h3>\n'
        f'{message}<br>\n'
        'Please go back and retry.\n'
        '</body>\n'
        '</html>\n'
    )
    return Response(page, mimetype='text/html')
