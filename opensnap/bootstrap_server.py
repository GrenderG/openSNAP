"""Bootstrap UDP server entrypoint."""

import logging

from opensnap.config import default_app_config
from opensnap.core.engine import SnapProtocolEngine
from opensnap.env_loader import load_env_file
from opensnap.logging_utils import configure_logging
from opensnap.udp_server import SnapUdpServer


def main() -> None:
    """CLI entrypoint for the bootstrap UDP service."""

    load_env_file()
    configure_logging(service_name='bootstrap')
    logger = logging.getLogger('opensnap.bootstrap')

    config = default_app_config()
    engine = SnapProtocolEngine(config=config, role='bootstrap')
    server = SnapUdpServer(
        config=config.server.bootstrap,
        engine=engine,
        tick_interval_seconds=config.server.tick_interval_seconds,
        logger_name='opensnap.bootstrap',
    )
    logger.info(
        'Starting openSNAP bootstrap server on %s:%d.',
        config.server.bootstrap.host,
        config.server.bootstrap.port,
    )
    try:
        server.run()
    except KeyboardInterrupt:
        logger.info('Received keyboard interrupt, shutting down bootstrap service.')
    except OSError:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
