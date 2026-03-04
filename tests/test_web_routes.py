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
        self.assertIn('Profile successfully retrieved.', text)
        self.assertIn('<!--INPUT-IDS-->alpha_9', text)
        self.assertTrue(text.endswith('<!--INPUT-IDS-->alpha_9\n'))

    def test_signup_route_accepts_maximum_length_credentials(self) -> None:
        response = self._client.get('/amweb/create_id.html?username=alpha_beta_1234&password=123456789012345')
        self.assertEqual(response.status_code, 200)
        text = response.get_data(as_text=True)
        self.assertIn('<!--INPUT-IDS-->alpha_beta_1234', text)

    def test_query_signup_route_supports_post(self) -> None:
        response = self._client.post('/amweb/create_id.html', data={'username': 'alpha_9', 'password': 'abc123'})
        self.assertEqual(response.status_code, 200)
        text = response.get_data(as_text=True)
        self.assertIn('<!--INPUT-IDS-->alpha_9', text)
        self.assertTrue(text.endswith('<!--INPUT-IDS-->alpha_9\n'))

    def test_invalid_signup_username_returns_error_page(self) -> None:
        response = self._client.get('/amweb/create_id_invalid!name.html?password=abc123')
        self.assertEqual(response.status_code, 200)
        text = response.get_data(as_text=True)
        self.assertIn('Login error', text)
        self.assertIn('Invalid username.', text)

    def test_overlong_signup_username_returns_error_page(self) -> None:
        response = self._client.get('/amweb/create_id.html?username=abcdefghijklmnop&password=abc123')
        self.assertEqual(response.status_code, 200)
        text = response.get_data(as_text=True)
        self.assertIn('Login error', text)
        self.assertIn('Invalid username.', text)

    def test_short_signup_username_returns_error_page(self) -> None:
        response = self._client.get('/amweb/create_id.html?username=abc&password=abcd')
        self.assertEqual(response.status_code, 200)
        text = response.get_data(as_text=True)
        self.assertIn('Login error', text)
        self.assertIn('Invalid username.', text)

    def test_leading_underscore_username_returns_error_page(self) -> None:
        response = self._client.get('/amweb/create_id.html?username=_alpha&password=abcd')
        self.assertEqual(response.status_code, 200)
        text = response.get_data(as_text=True)
        self.assertIn('Invalid username.', text)

    def test_trailing_underscore_username_returns_error_page(self) -> None:
        response = self._client.get('/amweb/create_id.html?username=alpha_&password=abcd')
        self.assertEqual(response.status_code, 200)
        text = response.get_data(as_text=True)
        self.assertIn('Invalid username.', text)

    def test_consecutive_underscore_username_returns_error_page(self) -> None:
        response = self._client.get('/amweb/create_id.html?username=alpha__beta&password=abcd')
        self.assertEqual(response.status_code, 200)
        text = response.get_data(as_text=True)
        self.assertIn('Invalid username.', text)

    def test_multiple_consecutive_underscore_username_returns_error_page(self) -> None:
        response = self._client.get('/amweb/create_id.html?username=alpha___beta&password=abcd')
        self.assertEqual(response.status_code, 200)
        text = response.get_data(as_text=True)
        self.assertIn('Invalid username.', text)

    def test_overlong_signup_password_returns_error_page(self) -> None:
        response = self._client.get('/amweb/create_id.html?username=tester&password=1234567890123456')
        self.assertEqual(response.status_code, 200)
        text = response.get_data(as_text=True)
        self.assertIn('Login error', text)
        self.assertIn('Invalid password.', text)

    def test_short_signup_password_returns_error_page(self) -> None:
        response = self._client.get('/amweb/create_id.html?username=tester&password=abc')
        self.assertEqual(response.status_code, 200)
        text = response.get_data(as_text=True)
        self.assertIn('Login error', text)
        self.assertIn('Invalid password.', text)

    def test_request_values_are_trimmed_before_validation(self) -> None:
        response = self._client.post('/amweb/create_id.html', data={'username': ' user_123 ', 'password': '  abcd  '})
        self.assertEqual(response.status_code, 200)
        text = response.get_data(as_text=True)
        self.assertIn('<!--INPUT-IDS-->user_123', text)

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
        self.assertIn('maxlength="15"', text)
        self.assertIn('action="create_id.html"', text)
        self.assertIn('type="submit"', text)

    def test_beta_root_index_route_is_available(self) -> None:
        response = self._client.get('/ftpublicbeta/reg/')
        self.assertEqual(response.status_code, 200)
        text = response.get_data(as_text=True)
        self.assertIn('openSNAP signup service', text)
        self.assertIn('action="create_id.html"', text)

    def test_beta_create_id_route_supports_registration(self) -> None:
        response = self._client.get('/ftpublicbeta/reg/create_id.html?username=betauser&password=abc123')
        self.assertEqual(response.status_code, 200)
        text = response.get_data(as_text=True)
        self.assertIn('Profile successfully retrieved.', text)
        self.assertIn('<!--INPUT-IDS-->betauser', text)
        self.assertTrue(text.endswith('<!--INPUT-IDS-->betauser\n'))

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

    def test_beta_amusa_alias_routes_are_available(self) -> None:
        page_expectations = {
            '/amusa/info.html': 'AM-USA-INFORMATION',
            '/amusa/rule.html': 'AM-USA-GAME-RULE',
            '/amusa/rank.html': 'am_rank',
            '/amusa/taboo.html': 'am_taboo',
        }

        for path, marker in page_expectations.items():
            response = self._client.get(path)
            self.assertEqual(response.status_code, 200)
            self.assertIn(marker, response.get_data(as_text=True))

        upload_response = self._client.post('/amusa/up.php', data={'crs': 'D'})
        self.assertEqual(upload_response.status_code, 200)

    def test_patch1_route_is_available(self) -> None:
        response = self._client.get('/amusa/patch1.html')
        self.assertEqual(response.status_code, 200)
        self.assertIn('This is test patch1.html file', response.get_data(as_text=True))

    def test_patch_v2_alias_route_is_available(self) -> None:
        response = self._client.get('/amusa/patch/2/am_patch1.html')
        self.assertEqual(response.status_code, 200)
        self.assertIn('This is test patch1.html file', response.get_data(as_text=True))

    def test_patch2_route_is_available(self) -> None:
        response = self._client.get('/amusa/patch2.html')
        self.assertEqual(response.status_code, 200)
        self.assertIn('This is test patch2.html file', response.get_data(as_text=True))

    def test_patch3_route_is_available(self) -> None:
        response = self._client.get('/amusa/patch3.html')
        self.assertEqual(response.status_code, 200)
        self.assertIn('This is test patch3.html file', response.get_data(as_text=True))

    def test_patch4_route_is_available(self) -> None:
        response = self._client.get('/amusa/patch4.html')
        self.assertEqual(response.status_code, 200)
        self.assertIn('This is test patch4.html file', response.get_data(as_text=True))

    def test_patch5_route_is_available(self) -> None:
        response = self._client.get('/amusa/patch5.html')
        self.assertEqual(response.status_code, 200)
        self.assertIn('This is test patch5.html file', response.get_data(as_text=True))

    def test_unknown_game_plugin_raises_value_error(self) -> None:
        with self.assertRaises(ValueError):
            create_web_app(
                WebServerConfig(
                    host='127.0.0.1',
                    port=18080,
                    game_plugin='unknown',
                )
            )

    def test_monsterhunter_web_plugin_registers_mh_specific_paths(self) -> None:
        app = create_web_app(
            WebServerConfig(
                host='127.0.0.1',
                port=18080,
                game_plugin='monsterhunter',
            )
        )
        app.testing = True
        client = app.test_client()

        for path in ('/mhweb/index.jsp', '/mheuweb/index.jsp', '/reweb/index.jsp'):
            response = client.get(path)
            self.assertEqual(response.status_code, 200)
            self.assertIn('openSNAP signup service', response.get_data(as_text=True))

        create_response = client.get('/mheuweb/create_id_hunter.html?password=abc123')
        self.assertEqual(create_response.status_code, 200)
        self.assertIn('<!--INPUT-IDS-->hunter', create_response.get_data(as_text=True))

        am_response = client.get('/amweb/index.jsp')
        self.assertEqual(am_response.status_code, 404)

    def test_existing_user_with_wrong_password_returns_error(self) -> None:
        response = self._client.post('/amweb/create_id.html', data={'username': 'test', 'password': 'wrong'})
        self.assertEqual(response.status_code, 200)
        text = response.get_data(as_text=True)
        self.assertIn('Login error', text)
        self.assertIn('Password mismatch for existing user.', text)


if __name__ == '__main__':
    unittest.main()
