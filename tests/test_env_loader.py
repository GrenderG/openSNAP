"""Environment file loader tests."""

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from opensnap.env_loader import load_env_file


class EnvLoaderTests(unittest.TestCase):
    """Verify `.env` parsing and load behavior."""

    def test_load_env_file_reads_values_and_respects_existing_env(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            env_path = Path(temp_dir) / '.env'
            env_path.write_text(
                '\n'.join(
                    (
                        'ALPHA=one',
                        "BETA='two words'",
                        'export GAMMA=three',
                        'ALPHA=should-not-win',
                    )
                ),
                encoding='utf-8',
            )

            with patch.dict(os.environ, {'ALPHA': 'existing'}, clear=True):
                load_env_file(str(env_path))
                self.assertEqual(os.getenv('ALPHA'), 'existing')
                self.assertEqual(os.getenv('BETA'), 'two words')
                self.assertEqual(os.getenv('GAMMA'), 'three')

    def test_load_env_file_supports_multiline_dict_values(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            env_path = Path(temp_dir) / '.env'
            env_path.write_text(
                '\n'.join(
                    (
                        'OPENSNAP_DNS_ENTRIES={',
                        '  # CAPCOM-AM titles',
                        '  "bootstrap.capcom-am.games.sega.net": "@default",',
                        '  "gameweb.capcom-am.games.sega.net": "@default",',
                        '  "regweb.capcom-am.games.sega.net": "@default"',
                        '}',
                    )
                ),
                encoding='utf-8',
            )

            with patch.dict(os.environ, {}, clear=True):
                load_env_file(str(env_path))
                value = os.getenv('OPENSNAP_DNS_ENTRIES')

        self.assertIsNotNone(value)
        assert value is not None
        self.assertIn('bootstrap.capcom-am.games.sega.net', value)
        self.assertIn('regweb.capcom-am.games.sega.net', value)
        self.assertNotIn('CAPCOM-AM titles', value)

    def test_missing_env_file_is_ignored(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            load_env_file('/tmp/opensnap-does-not-exist.env')
            self.assertNotIn('OPENSNAP_PORT', os.environ)

    def test_default_loader_backfills_from_env_dist_when_env_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            (temp_path / '.env.dist').write_text('FROM_DIST=yes\n', encoding='utf-8')

            current_dir = Path.cwd()
            try:
                os.chdir(temp_path)
                with patch.dict(os.environ, {}, clear=True):
                    load_env_file()
                    self.assertEqual(os.getenv('FROM_DIST'), 'yes')
            finally:
                os.chdir(current_dir)

    def test_default_loader_creates_env_from_dist_when_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            dist_path = temp_path / '.env.dist'
            dist_path.write_text('FROM_DIST=seeded\n', encoding='utf-8')

            current_dir = Path.cwd()
            try:
                os.chdir(temp_path)
                with patch.dict(os.environ, {}, clear=True):
                    self.assertFalse((temp_path / '.env').exists())
                    load_env_file()
                    env_path = temp_path / '.env'
                    self.assertTrue(env_path.exists())
                    self.assertEqual(env_path.read_text(encoding='utf-8'), dist_path.read_text(encoding='utf-8'))
                    self.assertEqual(os.getenv('FROM_DIST'), 'seeded')
            finally:
                os.chdir(current_dir)

    def test_default_loader_prefers_env_over_env_dist(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            (temp_path / '.env').write_text('SAMPLE_KEY=from_env\n', encoding='utf-8')
            (temp_path / '.env.dist').write_text('SAMPLE_KEY=from_dist\n', encoding='utf-8')

            current_dir = Path.cwd()
            try:
                os.chdir(temp_path)
                with patch.dict(os.environ, {}, clear=True):
                    load_env_file()
                    self.assertEqual(os.getenv('SAMPLE_KEY'), 'from_env')
            finally:
                os.chdir(current_dir)

    def test_default_loader_falls_back_to_repo_root_when_cwd_has_no_env_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_repo, tempfile.TemporaryDirectory() as temp_cwd:
            repo_path = Path(temp_repo)
            (repo_path / '.env').write_text('FROM_REPO=yes\n', encoding='utf-8')
            (repo_path / '.env.dist').write_text('FROM_DIST=no\n', encoding='utf-8')

            current_dir = Path.cwd()
            try:
                os.chdir(Path(temp_cwd))
                with patch('opensnap.env_loader._REPO_ROOT', repo_path):
                    with patch.dict(os.environ, {}, clear=True):
                        load_env_file()
                        self.assertEqual(os.getenv('FROM_REPO'), 'yes')
            finally:
                os.chdir(current_dir)


if __name__ == '__main__':
    unittest.main()
