"""Capcom APP process entrypoint tests: a bad title setting leaves only that title out."""

import os
import tempfile
import unittest
from unittest import mock

from opensnap_app.capcom import main as capcom_main
from opensnap_app.capcom import monsterhunter, outbreak, outbreak2
from opensnap_app.capcom.server import CapcomAppServer

MONSTER_HUNTER_BUILDS = {monsterhunter.APP_BUILD_NA, monsterhunter.APP_BUILD_EU}
OUTBREAK_BUILDS = {outbreak.APP_BUILD}
OUTBREAK2_BUILDS = {outbreak2.APP_BUILD_NA, outbreak2.APP_BUILD_EU}
TITLE_SETTING_PREFIXES = ('OPENSNAP_MH_', 'OPENSNAP_OUTBREAK2_')


class CapcomAppMainTests(unittest.TestCase):
    """`opensnap_app.capcom.main.main()` builds every title it can and logs the ones it cannot."""

    def _served(self, **settings: str) -> tuple[set[int], set[bytes]]:
        temp_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temp_directory.cleanup)
        environment = {
            'OPENSNAP_STORAGE_BACKEND': 'sqlite',
            'OPENSNAP_SQLITE_PATH': f'{temp_directory.name}/capcom.sqlite',
            'OPENSNAP_DATA_DIR': temp_directory.name,
            'OPENSNAP_CAPCOM_APP_HOST': '127.0.0.1',
            'OPENSNAP_CAPCOM_APP_PORT': '0',
            **settings,
        }
        served: list[tuple[set[int], set[bytes]]] = []
        with (
            mock.patch.dict(os.environ, environment),
            mock.patch.object(capcom_main, 'load_env_file'),
            mock.patch.object(capcom_main, 'configure_logging'),
            mock.patch.object(
                CapcomAppServer, 'serve_forever', autospec=True,
                side_effect=lambda server: served.append(
                    (set(server._titles), set(server._stamped_builds))  # noqa: SLF001
                ),
            ),
        ):
            for key in [key for key in os.environ if key.startswith(TITLE_SETTING_PREFIXES) and key not in settings]:
                del os.environ[key]
            capcom_main.main()
        self.assertEqual(len(served), 1)
        return served[0]

    def test_valid_settings_serve_every_title(self) -> None:
        builds, stamped = self._served()
        self.assertEqual(builds, MONSTER_HUNTER_BUILDS | OUTBREAK_BUILDS | OUTBREAK2_BUILDS)
        self.assertEqual(stamped, {monsterhunter.NA_PUBLIC_BETA_VERSION, outbreak.PUBLIC_BETA_VERSION})

    def test_bad_outbreak2_setting_leaves_only_file2_out(self) -> None:
        with self.assertLogs('opensnap_app.capcom', 'ERROR') as logs:
            builds, stamped = self._served(OPENSNAP_OUTBREAK2_AREA_KEY='BAD')
        self.assertTrue(any('Resident Evil Outbreak File#2 is not served' in line for line in logs.output))
        self.assertEqual(builds, MONSTER_HUNTER_BUILDS | OUTBREAK_BUILDS)
        self.assertEqual(stamped, {monsterhunter.NA_PUBLIC_BETA_VERSION, outbreak.PUBLIC_BETA_VERSION})

    def test_bad_monster_hunter_setting_leaves_only_monster_hunter_out(self) -> None:
        with self.assertLogs('opensnap_app.capcom', 'ERROR') as logs:
            builds, stamped = self._served(OPENSNAP_MH_WORLDS='not json')
        self.assertTrue(any('Monster Hunter is not served' in line for line in logs.output))
        self.assertEqual(builds, OUTBREAK_BUILDS | OUTBREAK2_BUILDS)
        self.assertEqual(stamped, {monsterhunter.NA_PUBLIC_BETA_VERSION, outbreak.PUBLIC_BETA_VERSION})

    def test_bad_monster_hunter_beta_setting_leaves_only_the_beta_out(self) -> None:
        with self.assertLogs('opensnap_app.capcom', 'ERROR') as logs:
            builds, stamped = self._served(OPENSNAP_MH_NA_BETA_WORLDS='not json')
        message = 'Monster Hunter NA public beta is not served: OPENSNAP_MH_NA_BETA_WORLDS'
        self.assertTrue(any(message in line for line in logs.output))
        self.assertEqual(builds, MONSTER_HUNTER_BUILDS | OUTBREAK_BUILDS | OUTBREAK2_BUILDS)
        self.assertEqual(stamped, {outbreak.PUBLIC_BETA_VERSION})

    def test_unreadable_ram_patch_leaves_both_monster_hunter_titles_out(self) -> None:
        # `OPENSNAP_MH_RAM_PATCH` is process-wide, so both Monster Hunter
        # deployments build their config from it.
        with self.assertLogs('opensnap_app.capcom', 'ERROR') as logs:
            builds, stamped = self._served(OPENSNAP_MH_RAM_PATCH='/nonexistent/patch.bin')
        self.assertTrue(any(
            'Monster Hunter is not served: OPENSNAP_MH_RAM_PATCH' in line for line in logs.output
        ))
        self.assertTrue(any(
            'Monster Hunter NA public beta is not served: OPENSNAP_MH_RAM_PATCH' in line
            for line in logs.output
        ))
        self.assertEqual(builds, OUTBREAK_BUILDS | OUTBREAK2_BUILDS)
        self.assertEqual(stamped, {outbreak.PUBLIC_BETA_VERSION})

    def test_unreadable_na_ram_patch_leaves_both_monster_hunter_titles_out(self) -> None:
        # `OPENSNAP_MH_RAM_PATCH_NA` is loaded by the same config builder, so
        # an unreadable NA blob drops the release and the beta as well.
        with self.assertLogs('opensnap_app.capcom', 'ERROR') as logs:
            builds, stamped = self._served(OPENSNAP_MH_RAM_PATCH_NA='/nonexistent/na.bin')
        self.assertTrue(any(
            'Monster Hunter is not served: OPENSNAP_MH_RAM_PATCH_NA' in line for line in logs.output
        ))
        self.assertTrue(any(
            'Monster Hunter NA public beta is not served: OPENSNAP_MH_RAM_PATCH_NA' in line
            for line in logs.output
        ))
        self.assertEqual(builds, OUTBREAK_BUILDS | OUTBREAK2_BUILDS)
        self.assertEqual(stamped, {outbreak.PUBLIC_BETA_VERSION})


if __name__ == '__main__':
    unittest.main()
