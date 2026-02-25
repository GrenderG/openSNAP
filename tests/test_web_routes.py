"""Web service route tests."""

import os
import tempfile
import unittest
from unittest.mock import patch

try:
    from opensnap_web.app import create_web_app
    from opensnap_web.config import WebServerConfig
except ModuleNotFoundError:  # pragma: no cover
    create_web_app = None
    WebServerConfig = None


@unittest.skipIf(create_web_app is None, 'Flask is not installed.')
class WebRouteTests(unittest.TestCase):
    """Validate known routes and unknown-route debug dumps."""

    def setUp(self) -> None:
        self._temp_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp_directory.cleanup)
        env_overrides = {
            'OPENSNAP_SQLITE_PATH': f'{self._temp_directory.name}/web.sqlite',
            'OPENSNAP_DEFAULT_USERS': 'test:1111',
        }
        patcher = patch.dict(os.environ, env_overrides, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)

        config = WebServerConfig(
            host='127.0.0.1',
            port=18080,
            game_plugin='automodellista',
        )
        app = create_web_app(config)
        app.testing = True
        self._client = app.test_client()

    def test_dynamic_signup_route_returns_expected_payload(self) -> None:
        response = self._client.get('/amweb/create_id_player1.html?password=pass1')
        self.assertEqual(response.status_code, 200)
        text = response.get_data(as_text=True)
        self.assertIn('<!--COMP-SIGNUP-->', text)
        self.assertIn('<!--INPUT-IDS-->player1', text)

    def test_query_signup_route_returns_expected_payload(self) -> None:
        response = self._client.get('/amweb/create_id.html?username=alpha_9&password=abc123')
        self.assertEqual(response.status_code, 200)
        text = response.get_data(as_text=True)
        self.assertIn('<!--INPUT-IDS-->alpha_9', text)

    def test_query_signup_route_supports_post(self) -> None:
        response = self._client.post('/amweb/create_id.html', data={'username': 'alpha_9', 'password': 'abc123'})
        self.assertEqual(response.status_code, 200)
        text = response.get_data(as_text=True)
        self.assertIn('<!--INPUT-IDS-->alpha_9', text)

    def test_invalid_signup_username_returns_error_page(self) -> None:
        response = self._client.get('/amweb/create_id_invalid!name.html?password=abc123')
        self.assertEqual(response.status_code, 200)
        text = response.get_data(as_text=True)
        self.assertIn('Login error', text)
        self.assertIn('Invalid username.', text)

    def test_overlong_signup_username_returns_error_page(self) -> None:
        response = self._client.get('/amweb/create_id.html?username=abcdefghijk&password=abc123')
        self.assertEqual(response.status_code, 200)
        text = response.get_data(as_text=True)
        self.assertIn('Login error', text)
        self.assertIn('Invalid username.', text)

    def test_overlong_signup_password_returns_error_page(self) -> None:
        response = self._client.get('/amweb/create_id.html?username=tester&password=123456789')
        self.assertEqual(response.status_code, 200)
        text = response.get_data(as_text=True)
        self.assertIn('Login error', text)
        self.assertIn('Invalid password.', text)

    def test_missing_signup_password_returns_error_page(self) -> None:
        response = self._client.get('/amweb/create_id.html?username=tester')
        self.assertEqual(response.status_code, 200)
        text = response.get_data(as_text=True)
        self.assertIn('Login error', text)
        self.assertIn('Invalid password.', text)

    def test_index_page_has_user_selected_signup_form(self) -> None:
        response = self._client.get('/amweb/index.jsp')
        self.assertEqual(response.status_code, 200)
        text = response.get_data(as_text=True)
        self.assertIn('name="username"', text)
        self.assertIn('name="password"', text)
        self.assertIn('maxlength="8"', text)
        self.assertIn('action="create_id.html"', text)
        self.assertIn('type="submit"', text)

    def test_login_php_route_is_available(self) -> None:
        response = self._client.get('/login.php')
        self.assertEqual(response.status_code, 200)
        text = response.get_data(as_text=True)
        self.assertIn('openSNAP signup service', text)

    def test_unknown_route_dumps_request_context(self) -> None:
        with self.assertLogs('opensnap.web', level='INFO') as captured:
            response = self._client.post('/amusa/not_implemented.php?mode=debug', data={'x': '1'})

        self.assertEqual(response.status_code, 404)
        dumped = '\n'.join(captured.output)
        self.assertIn('Unhandled route request received.', dumped)
        self.assertIn('path: /amusa/not_implemented.php', dumped)
        self.assertIn("query: {'mode': ['debug']}", dumped)
        self.assertIn("form: {'x': ['1']}", dumped)

    def test_am_up_php_route_returns_200(self) -> None:
        response = self._client.post('/amusa/am_up.php', data={'crs': 'D'})
        self.assertEqual(response.status_code, 200)

    def test_unknown_game_plugin_raises_value_error(self) -> None:
        with self.assertRaises(ValueError):
            create_web_app(
                WebServerConfig(
                    host='127.0.0.1',
                    port=18080,
                    game_plugin='unknown',
                )
            )

    def test_existing_user_with_wrong_password_returns_error(self) -> None:
        response = self._client.post('/amweb/create_id.html', data={'username': 'test', 'password': 'wrong'})
        self.assertEqual(response.status_code, 200)
        text = response.get_data(as_text=True)
        self.assertIn('Login error', text)
        self.assertIn('Password mismatch for existing user.', text)


if __name__ == '__main__':
    unittest.main()
