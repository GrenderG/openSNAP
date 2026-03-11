"""Fatal service entrypoint behavior tests."""

from io import StringIO
import logging
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from opensnap.logging_utils import exit_with_logged_os_error
import opensnap.bootstrap_server
import opensnap.game_server
import opensnap_dns.server
import opensnap_web.server


class FatalOSErrorLoggingTests(unittest.TestCase):
    """Verify fatal service `OSError` handling logs visibly before exit."""

    def test_exit_with_logged_os_error_logs_traceback_before_exit(self) -> None:
        stream = StringIO()
        logger = logging.getLogger('opensnap.test.fatal')
        original_handlers = list(logger.handlers)
        original_level = logger.level
        original_propagate = logger.propagate
        logger.handlers.clear()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(logging.Formatter('%(levelname)s:%(name)s:%(message)s'))
        logger.addHandler(handler)
        logger.setLevel(logging.ERROR)
        logger.propagate = False

        try:
            with self.assertRaises(SystemExit) as raised:
                try:
                    raise OSError('boom')
                except OSError as exc:
                    exit_with_logged_os_error(logger, service_name='game', error=exc)

            self.assertEqual(raised.exception.code, 1)
            output = stream.getvalue()
            self.assertIn('ERROR:opensnap.test.fatal:Fatal game service OSError: boom', output)
            self.assertIn('Traceback (most recent call last)', output)
            self.assertIn('OSError: boom', output)
        finally:
            logger.handlers.clear()
            logger.handlers.extend(original_handlers)
            logger.setLevel(original_level)
            logger.propagate = original_propagate

    def test_game_main_routes_fatal_os_error_through_logged_exit_helper(self) -> None:
        config = SimpleNamespace(
            server=SimpleNamespace(
                game=SimpleNamespace(host='127.0.0.1', port=1234),
                game_plugin='automodellista',
                tick_interval_seconds=0.05,
            )
        )
        plugin = SimpleNamespace(name='automodellista')
        error = OSError('bind failed')

        with patch('opensnap.game_server.load_env_file'):
            with patch('opensnap.game_server.configure_logging'):
                with patch('opensnap.game_server.default_app_config', return_value=config):
                    with patch('opensnap.game_server.create_game_plugin', return_value=plugin):
                        with patch('opensnap.game_server.SnapProtocolEngine'):
                            with patch('opensnap.game_server.SnapUdpServer') as server_cls:
                                with patch(
                                    'opensnap.game_server.exit_with_logged_os_error',
                                    side_effect=SystemExit(1),
                                ) as exit_helper:
                                    server_cls.return_value.run.side_effect = error
                                    with self.assertRaises(SystemExit):
                                        opensnap.game_server.main()

        exit_helper.assert_called_once()
        self.assertEqual(exit_helper.call_args.kwargs['service_name'], 'game')
        self.assertIs(exit_helper.call_args.kwargs['error'], error)

    def test_bootstrap_main_routes_fatal_os_error_through_logged_exit_helper(self) -> None:
        config = SimpleNamespace(
            server=SimpleNamespace(
                bootstrap=SimpleNamespace(host='127.0.0.1', port=1234),
                tick_interval_seconds=0.05,
            )
        )
        error = OSError('bind failed')

        with patch('opensnap.bootstrap_server.load_env_file'):
            with patch('opensnap.bootstrap_server.configure_logging'):
                with patch('opensnap.bootstrap_server.default_app_config', return_value=config):
                    with patch('opensnap.bootstrap_server.SnapProtocolEngine'):
                        with patch('opensnap.bootstrap_server.SnapUdpServer') as server_cls:
                            with patch(
                                'opensnap.bootstrap_server.exit_with_logged_os_error',
                                side_effect=SystemExit(1),
                            ) as exit_helper:
                                server_cls.return_value.run.side_effect = error
                                with self.assertRaises(SystemExit):
                                    opensnap.bootstrap_server.main()

        exit_helper.assert_called_once()
        self.assertEqual(exit_helper.call_args.kwargs['service_name'], 'bootstrap')
        self.assertIs(exit_helper.call_args.kwargs['error'], error)

    def test_dns_main_routes_fatal_os_error_through_logged_exit_helper(self) -> None:
        config = SimpleNamespace(
            host='127.0.0.1',
            port=1234,
            entries={},
        )
        error = OSError('bind failed')

        with patch('opensnap_dns.server.load_env_file'):
            with patch('opensnap_dns.server.configure_logging'):
                with patch('opensnap_dns.server.default_dns_server_config', return_value=config):
                    with patch('opensnap_dns.server.SnapDnsServer') as server_cls:
                        with patch(
                            'opensnap_dns.server.exit_with_logged_os_error',
                            side_effect=SystemExit(1),
                        ) as exit_helper:
                            server_cls.return_value.run.side_effect = error
                            with self.assertRaises(SystemExit):
                                opensnap_dns.server.main()

        exit_helper.assert_called_once()
        self.assertEqual(exit_helper.call_args.kwargs['service_name'], 'dns')
        self.assertIs(exit_helper.call_args.kwargs['error'], error)

    def test_web_main_routes_fatal_os_error_through_logged_exit_helper(self) -> None:
        config = SimpleNamespace(
            host='127.0.0.1',
            port=8080,
            game_plugin='automodellista',
            https_enabled=False,
            https_certfile='',
            https_keyfile='',
        )
        error = OSError('bind failed')

        with patch('opensnap_web.server.load_env_file'):
            with patch('opensnap_web.server.configure_logging'):
                with patch('opensnap_web.server.default_web_server_config', return_value=config):
                    with patch('opensnap_web.server.create_web_app', return_value=object()):
                        with patch('opensnap_web.server._enable_web_reuse_address'):
                            with patch('opensnap_web.server.make_server', side_effect=error):
                                with patch(
                                    'opensnap_web.server.exit_with_logged_os_error',
                                    side_effect=SystemExit(1),
                                ) as exit_helper:
                                    with self.assertRaises(SystemExit):
                                        opensnap_web.server.main()

        exit_helper.assert_called_once()
        self.assertEqual(exit_helper.call_args.kwargs['service_name'], 'web')
        self.assertIs(exit_helper.call_args.kwargs['error'], error)


if __name__ == '__main__':
    unittest.main()
