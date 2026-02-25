"""Storage factory tests."""

from dataclasses import replace
import tempfile
import unittest

from opensnap.config import StorageConfig, default_app_config
from opensnap.storage.factory import create_storage


class StorageFactoryTests(unittest.TestCase):
    """Verify SQLite-only storage behavior."""

    def test_create_storage_rejects_non_sqlite_backend(self) -> None:
        config = replace(
            default_app_config(),
            storage=StorageConfig(backend='memory', sqlite_path='opensnap.db'),
        )

        with self.assertRaises(ValueError):
            create_storage(config)

    def test_create_storage_accepts_sqlite_backend(self) -> None:
        with tempfile.TemporaryDirectory() as temp_directory:
            config = replace(
                default_app_config(),
                storage=StorageConfig(backend='sqlite', sqlite_path=f'{temp_directory}/factory.sqlite'),
            )
            bundle = create_storage(config)

        self.assertIsNotNone(bundle.accounts)
        self.assertIsNotNone(bundle.sessions)
        self.assertIsNotNone(bundle.lobbies)
        self.assertIsNotNone(bundle.rooms)


if __name__ == '__main__':
    unittest.main()
