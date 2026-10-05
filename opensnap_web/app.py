"""Flask application serving the games' web pages."""

from flask import Flask, Response

from opensnap.config import default_app_config
from opensnap.storage.factory import create_storage
from opensnap_web.common import dump_request, text_response
from opensnap_web.config import WebServerConfig, default_web_server_config
from opensnap_web.games import select_web_modules


def create_web_app(config: WebServerConfig | None = None) -> Flask:
    """Build the app with the selected game modules over one shared-store connection."""

    web_config = config or default_web_server_config()
    modules = select_web_modules(web_config.game_plugin)
    storage = create_storage(default_app_config())
    app = Flask(__name__)
    for module in modules:
        app.register_blueprint(module.blueprint(storage))

    @app.errorhandler(404)
    def unknown_route(_error: Exception) -> Response:
        dump_request('Unhandled route request received.')
        return text_response('Not Found\n'), 404

    return app
