"""WSGI entrypoint tests."""

import unittest

try:
    from opensnap_web.wsgi import app, application
except ModuleNotFoundError:  # pragma: no cover
    app = None
    application = None


@unittest.skipIf(app is None, 'Flask is not installed.')
class WebWsgiTests(unittest.TestCase):
    """Ensure WSGI callables are exposed."""

    def test_app_and_application_callables_exist(self) -> None:
        self.assertIsNotNone(app)
        self.assertIs(app, application)
        self.assertTrue(callable(app))


if __name__ == '__main__':
    unittest.main()
