"""Game UDP server entrypoint."""

import logging

from opensnap.config import default_app_config
from opensnap.core.engine import SnapProtocolEngine
from opensnap.env_loader import load_env_file
from opensnap.logging_utils import configure_logging, exit_with_logged_os_error
from opensnap.plugins import create_game_plugin
from opensnap.udp_server import SnapUdpServer


def main() -> None:
    """CLI entrypoint for the game UDP service."""

    load_env_file()
    configure_logging(service_name='game')
    logger = logging.getLogger('opensnap.game')

    config = default_app_config()
    plugin = create_game_plugin(config.server.game_plugin)
    engine = SnapProtocolEngine(config=config, plugin=plugin, role='game')
    server = SnapUdpServer(
        config=config.server.game,
        engine=engine,
        tick_interval_seconds=config.server.tick_interval_seconds,
        logger_name='opensnap.game',
    )
    logger.info(
        'Starting openSNAP game server on %s:%d using plugin %s.',
        config.server.game.host,
        config.server.game.port,
        plugin.name,
    )
    try:
        server.run()
    except KeyboardInterrupt:
        logger.info('Received keyboard interrupt, shutting down game service.')
    except OSError as exc:
        exit_with_logged_os_error(logger, service_name='game', error=exc)


if __name__ == '__main__':
    main()
