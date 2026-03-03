"""Launcher dispatch tests."""

import sys
import types
import unittest
from unittest.mock import Mock, patch

import run


class RunLauncherTests(unittest.TestCase):
    """Verify `run.py` dispatches to the expected service entrypoints."""

    def test_default_service_dispatches_to_game(self) -> None:
        game_main = Mock()

        with patch('run.load_env_file') as load_env:
            with patch('run.argparse.ArgumentParser.parse_args', return_value=types.SimpleNamespace(service='game')):
                with patch.dict(
                    sys.modules,
                    {'opensnap.game_server': _module_with_main('opensnap.game_server', game_main)},
                ):
                    run.main()

        load_env.assert_called_once()
        game_main.assert_called_once_with()

    def test_bootstrap_service_dispatches_to_bootstrap_server(self) -> None:
        bootstrap_main = Mock()

        with patch('run.load_env_file'):
            with patch(
                'run.argparse.ArgumentParser.parse_args',
                return_value=types.SimpleNamespace(service='bootstrap'),
            ):
                with patch.dict(
                    sys.modules,
                    {'opensnap.bootstrap_server': _module_with_main('opensnap.bootstrap_server', bootstrap_main)},
                ):
                    run.main()

        bootstrap_main.assert_called_once_with()


def _module_with_main(name: str, main: Mock) -> types.ModuleType:
    """Build a minimal module object exposing one mocked `main`."""

    module = types.ModuleType(name)
    module.main = main  # type: ignore[attr-defined]
    return module


if __name__ == '__main__':
    unittest.main()
