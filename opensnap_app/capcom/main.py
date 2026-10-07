"""Capcom APP process entrypoint: `python run.py app capcom`."""

import logging
import os

from opensnap.config import AppConfig, default_app_config
from opensnap.env_loader import load_env_file
from opensnap.logging_utils import configure_logging, exit_with_logged_os_error
from opensnap.plugins.monsterhunter.directory import read_directory
from opensnap.plugins.monsterhunter.profile import MONSTER_HUNTER_NA, MONSTER_HUNTER_NA_BETA, MonsterHunterProfile
from opensnap.storage.factory import create_storage
from opensnap.storage.interfaces import StorageBundle
from opensnap_app.capcom import monsterhunter, outbreak
from opensnap_app.capcom.server import CapcomAppServer

DEFAULT_HOST = '0.0.0.0'
# Every Capcom title hardcodes this port.
DEFAULT_PORT = 10127


def main() -> None:
    load_env_file()
    configure_logging(service_name='capcom-app')
    logger = logging.getLogger('opensnap_app.capcom')

    config = default_app_config()
    storage = create_storage(config)
    monster_hunter = _monster_hunter_service(config, storage, MONSTER_HUNTER_NA)
    monster_hunter_na_beta = _monster_hunter_service(config, storage, MONSTER_HUNTER_NA_BETA)
    resident_evil_outbreak = outbreak.AppService(
        outbreak.read_app_service_config(), accounts=storage.accounts, records=storage.records
    )
    server = CapcomAppServer(
        host=os.getenv('OPENSNAP_CAPCOM_APP_HOST', '').strip() or DEFAULT_HOST,
        port=int(os.getenv('OPENSNAP_CAPCOM_APP_PORT', '').strip() or DEFAULT_PORT),
        titles={
            monsterhunter.APP_BUILD_NA: monster_hunter,
            monsterhunter.APP_BUILD_EU: monster_hunter,
            outbreak.APP_BUILD: resident_evil_outbreak,
        },
        stamped_builds={monsterhunter.NA_PUBLIC_BETA_VERSION: monster_hunter_na_beta},
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info('Received keyboard interrupt, shutting down Capcom APP service.')
    except OSError as exc:
        exit_with_logged_os_error(logger, service_name='capcom-app', error=exc)
    finally:
        storage.close()


def _monster_hunter_service(
    config: AppConfig, storage: StorageBundle, profile: MonsterHunterProfile
) -> monsterhunter.AppService:
    """One Monster Hunter deployment: its own players, Worlds and data folder."""

    return monsterhunter.AppService(
        config=monsterhunter.read_app_service_config(config, profile),
        flows=monsterhunter.AppFlowTracker(),
        directory=read_directory(
            max_players_per_town=config.server.max_players_per_room,
            environment_key=profile.worlds_environment_key,
        ),
        online_players=storage.online_players,
        records=storage.records,
    )
