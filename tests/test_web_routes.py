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
        # The browser keeps 10 ID characters; clients type up to 15 password characters.
        response = self._client.get('/amweb/create_id.html?username=grender_12&password=123456789012345')
        self.assertIn('<!--INPUT-IDS-->grender_12', response.get_data(as_text=True))
        response = self._client.get('/amweb/create_id.html?username=grender_123&password=abcd')
        self.assertIn('Invalid username.', response.get_data(as_text=True))

    def test_monster_hunter_signup_uses_the_same_limits(self) -> None:
        app = create_web_app(WebServerConfig(host='127.0.0.1', port=18080, game_plugin='monsterhunter'))
        client = app.test_client()
        created = client.get('/mhweb/create_id.html?username=hunter_123&password=abcd')
        self.assertIn('<!--INPUT-IDS-->hunter_123', created.get_data(as_text=True))
        self.assertIn('maxlength="10"', client.get('/mhweb/index.jsp').get_data(as_text=True))

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
        self.assertIn('name="username" size="10" maxlength="10"', text)
        self.assertIn('name="password" size="15" maxlength="15"', text)
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

    def test_release_amusa_routes_are_available(self) -> None:
        page_expectations = {
            '/amusa/am_info.html': 'AM-USA-INFORMATION',
            '/amusa/am_rule.html': 'AM-USA-GAME-RULE',
            '/amusa/am_rank.html': 'AM-USA-RANKING',
            '/amusa/am_taboo.html': 'am_taboo',
        }

        for path, marker in page_expectations.items():
            response = self._client.get(path)
            self.assertEqual(response.status_code, 200)
            self.assertIn(marker, response.get_data(as_text=True))

        upload_response = self._client.post('/amusa/am_up.php', data={'crs': 'D'})
        self.assertEqual(upload_response.status_code, 200)

    def test_ranking_uploads_fill_the_ranking_page_best_first(self) -> None:
        def utl(login: str, team: str, lap_ms: int) -> str:
            return f'{login:<15}{team:<15}{lap_ms:010d}'

        self._client.get('/amweb/create_id.html?username=racer&password=abc123')
        uploads = [
            ('D', utl('test', 'TEAM"A', 83_456)),
            ('D', utl('racer', '', 81_002)),
            # A slower lap does not replace the player's best.
            ('D', utl('racer', '', 90_000)),
            ('R', utl('test', 'TEAM"A', 125_007)),
            # Ignored: unknown account, club meeting (no lap), lap 0, malformed.
            ('D', utl('nobody', '', 1)),
            ('S', utl('test', '', 0)),
            ('A', utl('test', '', 0)),
            ('A', 'short'),
        ]
        for course, value in uploads:
            response = self._client.post('/amusa/am_up.php', data={'crs': course, 'utl': value, 'opt': '-' * 32})
            self.assertEqual(response.status_code, 200)

        page = self._client.get('/amusa/am_rank.html').get_data(as_text=True)
        self.assertEqual(
            page,
            '<html><head>\n<!--AM-USA-RANKING-->\n</head>\n<!--\n<CSV>\n'
            '"D","racer","","0121002",\n'
            '"D","test","TEAMA","0123456",\n'
            '"R","test","TEAMA","0205007"\n'
            '</CSV>\n-->\n</html>\n',
        )

    def test_patch_routes_serve_empty_pages(self) -> None:
        for number in range(1, 6):
            for path in (f'/amusa/patch{number}.html', f'/amusa/patch/2/am_patch{number}.html'):
                response = self._client.get(path)
                self.assertEqual((response.status_code, response.data), (200, b''), path)

    def test_unknown_game_plugin_raises_value_error(self) -> None:
        with self.assertRaises(ValueError):
            create_web_app(
                WebServerConfig(
                    host='127.0.0.1',
                    port=18080,
                    game_plugin='unknown',
                )
            )

    def test_generic_aliases_are_rejected(self) -> None:
        for alias in ('auto', 'all'):
            with self.assertRaises(ValueError):
                create_web_app(
                    WebServerConfig(
                        host='127.0.0.1',
                        port=18080,
                        game_plugin=alias,
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

    def test_automodellista_beta1_web_plugin_serves_beta1_rule_payload(self) -> None:
        app = create_web_app(
            WebServerConfig(
                host='127.0.0.1',
                port=18080,
                game_plugin='automodellista_beta1',
            )
        )
        app.testing = True
        client = app.test_client()

        rule_response = client.get('/amusa/rule.html')
        self.assertEqual(rule_response.status_code, 200)
        rule_page = rule_response.get_data(as_text=True)
        self.assertIn('AM-USA-GAME-RULE', rule_page)
        self.assertIn('"00000a0000080f00010000000000000000280000000000000000"', rule_page)

        taboo_response = client.get('/amusa/taboo.html')
        self.assertEqual(taboo_response.status_code, 404)

    def test_generic_web_profile_routes_am_pages_from_all_modules(self) -> None:
        app = create_web_app(
            WebServerConfig(
                host='127.0.0.1',
                port=18080,
                game_plugin='generic',
            )
        )
        app.testing = True
        client = app.test_client()

        release_rule = client.get('/amusa/am_rule.html')
        self.assertEqual(release_rule.status_code, 200)
        self.assertIn('AM-USA-GAME-RULE', release_rule.get_data(as_text=True))
        self.assertIn(
            '"00000a0000080f000100000000000000000000280000000000000000"',
            release_rule.get_data(as_text=True),
        )

        beta_rule = client.get('/amusa/rule.html')
        self.assertEqual(beta_rule.status_code, 200)
        self.assertIn(
            '"00000a0000080f00010000000000000000280000000000000000"',
            beta_rule.get_data(as_text=True),
        )

        release_rank = client.get('/amusa/am_rank.html')
        self.assertEqual(release_rank.status_code, 200)

        beta_rank = client.get('/amusa/rank.html')
        self.assertEqual(beta_rank.status_code, 200)

    def test_generic_web_profile_keeps_monsterhunter_routes_available(self) -> None:
        app = create_web_app(
            WebServerConfig(
                host='127.0.0.1',
                port=18080,
                game_plugin='generic',
            )
        )
        app.testing = True
        client = app.test_client()

        response = client.get('/mhweb/index.jsp')
        self.assertEqual(response.status_code, 200)
        self.assertIn('openSNAP signup service', response.get_data(as_text=True))

    def test_existing_user_with_wrong_password_returns_error(self) -> None:
        response = self._client.post('/amweb/create_id.html', data={'username': 'test', 'password': 'wrong'})
        self.assertEqual(response.status_code, 200)
        text = response.get_data(as_text=True)
        self.assertIn('Login error', text)
        self.assertIn('Password mismatch for existing user.', text)


if __name__ == '__main__':
    unittest.main()
