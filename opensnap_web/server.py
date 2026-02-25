"""Launcher for openSNAP web bootstrap service."""

import logging

from opensnap.env_loader import load_env_file
from opensnap.logging_utils import configure_logging
from opensnap_web.app import create_web_app
from opensnap_web.config import default_web_server_config


def main() -> None:
    """Run Flask web service."""

    load_env_file()
    configure_logging()
    logger = logging.getLogger('opensnap.web')
    config = default_web_server_config()
    app = create_web_app(config)
    logger.info(
        'openSNAP web listening on %s:%d using plugin %s.',
        config.host,
        config.port,
        config.game_plugin,
    )
    app.run(
        host=config.host,
        port=config.port,
        debug=False,
        use_reloader=False,
    )


if __name__ == '__main__':
    main()
