"""Flask web application for bootstrap and game web routes."""

import logging

from flask import Flask, Response, request

from opensnap_web.config import WebServerConfig, default_web_server_config
from opensnap_web.games.base import GameWebModule
from opensnap_web.games.base import WebRouteTools
from opensnap_web.games.registry import create_game_web_module

LOGGER = logging.getLogger('opensnap.web')

# Canonical selector for all-modules web mode.
GENERIC_NAME = 'generic'

# Generic module registration order used when `OPENSNAP_WEB_GAME_PLUGIN=generic`.
# Keep Beta1 before release so non-`am_*` AM-USA routes stay deterministic.
WEB_PLUGIN_NAMES = (
    'automodellista_beta1',
    'automodellista',
    'monsterhunter',
)


def create_web_app(config: WebServerConfig | None = None) -> Flask:
    """Create configured Flask application."""

    web_config = config or default_web_server_config()
    modules = _resolve_game_modules(web_config.game_plugin)
    app = Flask(__name__)
    tools = WebRouteTools(
        dump_request=_dump_request,
        html_response=_html_response,
    )

    for game_module in modules:
        game_module.register_routes(app, web_config, tools)

    @app.errorhandler(404)
    def unknown_route(_error: Exception) -> Response:
        _dump_request('Unhandled route request received.')
        return Response('Not Found\n', status=404, mimetype='text/plain')

    return app


def _resolve_game_modules(plugin_name: str) -> tuple[GameWebModule, ...]:
    """Resolve one explicit web profile or the generic all-module set."""

    normalized = plugin_name.strip().lower()
    if normalized == GENERIC_NAME:
        return tuple(create_game_web_module(module_name) for module_name in WEB_PLUGIN_NAMES)
    # Explicit plugin selection: register only that module (child+parent routes).
    return (create_game_web_module(normalized),)


def _html_response(content: str) -> Response:
    """Build HTML response with deterministic content type."""

    return Response(content, mimetype='text/html')


def _dump_request(title: str) -> None:
    """Print detailed request diagnostics for reverse-engineering."""

    # Keep cached body so Flask can still populate request.form for tests/logging.
    raw_body = request.get_data(cache=True)
    body_preview = raw_body.decode('utf-8', errors='replace')
    headers = {key: value for key, value in request.headers.items()}
    args = {key: request.args.getlist(key) for key in request.args}
    form = {key: request.form.getlist(key) for key in request.form}
    LOGGER.info('%s', title)
    LOGGER.info('  method: %s', request.method)
    LOGGER.info('  path: %s', request.path)
    LOGGER.info('  full_path: %s', request.full_path)
    LOGGER.info('  url: %s', request.url)
    LOGGER.info('  remote_addr: %s', request.remote_addr)
    LOGGER.info('  host: %s', request.host)
    LOGGER.info('  query: %s', args)
    LOGGER.info('  form: %s', form)
    LOGGER.info('  headers: %s', headers)
    LOGGER.info('  body_len: %d', len(raw_body))
    LOGGER.info('  body_preview: %r', body_preview)
