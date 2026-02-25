"""Flask web application for bootstrap and game web routes."""

import logging

from flask import Flask, Response, request

from opensnap_web.config import WebServerConfig, default_web_server_config
from opensnap_web.games.base import WebRouteTools
from opensnap_web.games.registry import create_game_web_module

LOGGER = logging.getLogger('opensnap.web')


def create_web_app(config: WebServerConfig | None = None) -> Flask:
    """Create configured Flask application."""

    web_config = config or default_web_server_config()
    app = Flask(__name__)

    game_module = create_game_web_module(web_config.game_plugin)
    tools = WebRouteTools(
        dump_request=_dump_request,
        html_response=_html_response,
    )
    game_module.register_routes(app, web_config, tools)

    @app.errorhandler(404)
    def unknown_route(_error: Exception) -> Response:
        _dump_request('Unhandled route request received.')
        return Response('Not Found\n', status=404, mimetype='text/plain')

    return app


def _html_response(content: str) -> Response:
    """Build HTML response with deterministic content type."""

    return Response(content, mimetype='text/html')


def _dump_request(title: str) -> None:
    """Print detailed request diagnostics for reverse-engineering."""

    raw_body = request.get_data(cache=False)
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
