"""Launcher for openSNAP web bootstrap service."""

from dataclasses import replace
import logging
import ssl
import threading

from opensnap.env_loader import load_env_file
from opensnap.logging_utils import configure_logging
from opensnap_web.app import create_web_app
from opensnap_web.config import WebServerConfig, default_web_server_config
from werkzeug.serving import BaseWSGIServer, ThreadedWSGIServer, make_server


def main(*, web_plugin: str | None = None) -> None:
    """Run Flask web service."""

    load_env_file()
    configure_logging(service_name='web')
    logger = logging.getLogger('opensnap.web')
    config = default_web_server_config()
    if web_plugin is not None:
        normalized = web_plugin.strip().lower()
        if normalized:
            config = replace(config, game_plugin=normalized)
    app = create_web_app(config)
    _enable_web_reuse_address(logger)
    http_server = None
    https_server = None
    try:
        http_server = make_server(
            config.host,
            config.port,
            app,
            threaded=True,
        )
        logger.info(
            'Starting openSNAP web HTTP on %s:%d using plugin %s.',
            config.host,
            config.port,
            config.game_plugin,
        )
        https_server = _start_optional_https_server(
            logger=logger,
            config=config,
            app=app,
        )
        http_server.serve_forever()
    except KeyboardInterrupt:
        logger.info('Received keyboard interrupt, shutting down web service.')
    except OSError as exc:
        logger.error('Failed to bind/start web service: %s', exc)
        raise SystemExit(1) from exc
    finally:
        if https_server is not None:
            https_server.shutdown()
            https_server.server_close()
        if http_server is not None:
            http_server.shutdown()
            http_server.server_close()


def _start_optional_https_server(
    *,
    logger: logging.Logger,
    config: WebServerConfig,
    app: object,
):
    """Start the optional HTTPS listener used by the post-game web flow."""

    if not config.https_enabled:
        if config.https_certfile or config.https_keyfile:
            logger.warning(
                'HTTPS listener disabled because both OPENSNAP_WEB_HTTPS_CERTFILE '
                'and OPENSNAP_WEB_HTTPS_KEYFILE are required.'
            )
        else:
            logger.warning(
                'HTTPS listener disabled. Ranking URLs use '
                'https://rankweb..., so configure a certificate and key to '
                'serve that path locally.'
            )
        return None

    ssl_context = _build_ssl_context(config)
    https_server = make_server(
        config.https_host,
        config.https_port,
        app,
        threaded=True,
        ssl_context=ssl_context,
    )
    thread = threading.Thread(
        target=https_server.serve_forever,
        name='opensnap-web-https',
        daemon=True,
    )
    thread.start()
    logger.info(
        'Starting openSNAP web HTTPS on %s:%d using plugin %s.',
        config.https_host,
        config.https_port,
        config.game_plugin,
    )
    return https_server


def _build_ssl_context(config: WebServerConfig) -> ssl.SSLContext:
    """Build the HTTPS TLS context for the optional rankweb listener."""

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(
        certfile=config.https_certfile,
        keyfile=config.https_keyfile,
    )
    return context


def _enable_web_reuse_address(logger: logging.Logger) -> None:
    """Enable HTTP listener address reuse for Werkzeug servers."""

    BaseWSGIServer.allow_reuse_address = True
    ThreadedWSGIServer.allow_reuse_address = True
    logger.debug('Enabled SO_REUSEADDR for Werkzeug HTTP servers.')


if __name__ == '__main__':
    main()
