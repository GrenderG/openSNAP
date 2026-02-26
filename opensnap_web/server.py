"""Launcher for openSNAP web bootstrap service."""

import logging

from opensnap.env_loader import load_env_file
from opensnap.logging_utils import configure_logging
from opensnap_web.app import create_web_app
from opensnap_web.config import default_web_server_config
from werkzeug.serving import BaseWSGIServer, ThreadedWSGIServer


def main() -> None:
    """Run Flask web service."""

    load_env_file()
    configure_logging()
    logger = logging.getLogger('opensnap.web')
    config = default_web_server_config()
    app = create_web_app(config)
    logger.info(
        'Starting openSNAP web on %s:%d using plugin %s.',
        config.host,
        config.port,
        config.game_plugin,
    )
    _enable_web_reuse_address(logger)
    try:
        app.run(
            host=config.host,
            port=config.port,
            debug=False,
            use_reloader=False,
            threaded=True,
        )
    except OSError as exc:
        logger.error(
            'Failed to bind/start web service on %s:%d: %s',
            config.host,
            config.port,
            exc,
        )
        raise SystemExit(1) from exc


def _enable_web_reuse_address(logger: logging.Logger) -> None:
    """Enable HTTP listener address reuse for Werkzeug servers."""

    BaseWSGIServer.allow_reuse_address = True
    ThreadedWSGIServer.allow_reuse_address = True
    logger.debug('Enabled SO_REUSEADDR for Werkzeug HTTP servers.')


if __name__ == '__main__':
    main()
