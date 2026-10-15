"""Every SN@P game gets server keepalives."""

import unittest

from opensnap.plugins.registry import PLUGIN_FACTORIES


class PluginKeepaliveTests(unittest.TestCase):
    """Every served build is proven to ACK a reliable command 0 without running a callback."""

    def test_every_game_sends_keepalives(self) -> None:
        intervals = {name: plugin.keepalive_interval_seconds for name, plugin in PLUGIN_FACTORIES.items()}

        self.assertEqual(
            intervals,
            {
                'automodellista': 20.0,
                'automodellista_beta1': 20.0,
                'monsterhunter': 20.0,
                'monsterhunter_na_beta': 20.0,
                'outbreak': 20.0,
                'outbreak2': 20.0,
            },
        )

    def test_only_beta1_waits_longer_for_acks(self) -> None:
        timeouts = {name: plugin.reliable_timeout_seconds for name, plugin in PLUGIN_FACTORIES.items()}

        self.assertEqual(timeouts.pop('automodellista_beta1'), 300.0)
        self.assertEqual(set(timeouts.values()), {60.0})


if __name__ == '__main__':
    unittest.main()
