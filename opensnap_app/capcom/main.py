"""Capcom APP process entrypoint: `python run.py app capcom`."""

from collections.abc import Callable
import logging
import os

from opensnap.config import AppConfig, default_app_config
from opensnap.env_loader import load_env_file
from opensnap.logging_utils import configure_logging, exit_with_logged_os_error
from opensnap.plugins.monsterhunter.directory import read_directory
from opensnap.plugins.monsterhunter.profile import MONSTER_HUNTER_NA, MONSTER_HUNTER_NA_BETA, MonsterHunterProfile
from opensnap.plugins.monsterhunter.webhooks import MonsterHunterWebhooks
from opensnap.plugins.outbreak2.directory import read_directory as read_outbreak2_directory
from opensnap.storage.factory import create_storage
from opensnap.storage.interfaces import StorageBundle
from opensnap.webhooks import DayChangeTimer, WebhookDispatcher
from opensnap_app.capcom import monsterhunter, outbreak, outbreak2
from opensnap_app.capcom.server import CapcomAppServer, TitleService

DEFAULT_HOST = '0.0.0.0'
# Every Capcom title hardcodes this port.
DEFAULT_PORT = 10127

logger = logging.getLogger('opensnap_app.capcom')


def main() -> None:
    load_env_file()
    configure_logging(service_name='capcom-app')

    config = default_app_config()
    storage = create_storage(config)
    dispatcher = WebhookDispatcher.from_env()
    titles: dict[int, TitleService] = {}
    stamped_builds: dict[bytes, TitleService] = {}
    _add_title(
        titles, (monsterhunter.APP_BUILD_NA, monsterhunter.APP_BUILD_EU), 'Monster Hunter',
        lambda: _monster_hunter_service(config, storage, MONSTER_HUNTER_NA, dispatcher, flows=None),
    )
    _add_title(
        stamped_builds, (monsterhunter.NA_PUBLIC_BETA_VERSION,), 'Monster Hunter NA public beta',
        lambda: _monster_hunter_service(
            config, storage, MONSTER_HUNTER_NA_BETA, dispatcher, flows=monsterhunter.AppFlowTracker()
        ),
    )
    _add_title(titles, (outbreak.APP_BUILD,), 'Resident Evil Outbreak', lambda: _outbreak_service(storage))
    _add_title(
        stamped_builds, (outbreak.PUBLIC_BETA_VERSION,), 'Resident Evil Outbreak NA public beta',
        lambda: _outbreak_service(storage, public_beta=True),
    )
    _add_title(
        titles, (outbreak2.APP_BUILD_NA, outbreak2.APP_BUILD_EU), 'Resident Evil Outbreak File#2',
        lambda: _outbreak2_service(config, storage),
    )
    server = CapcomAppServer(
        host=os.getenv('OPENSNAP_CAPCOM_APP_HOST', '').strip() or DEFAULT_HOST,
        port=int(os.getenv('OPENSNAP_CAPCOM_APP_PORT', '').strip() or DEFAULT_PORT),
        titles=titles,
        stamped_builds=stamped_builds,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info('Received keyboard interrupt, shutting down Capcom APP service.')
    except OSError as exc:
        exit_with_logged_os_error(logger, service_name='capcom-app', error=exc)
    finally:
        storage.close()


def _add_title[K](
    services: dict[K, TitleService], keys: tuple[K, ...], name: str, build: Callable[[], TitleService]
) -> None:
    """Serve one title under its keys; a bad setting leaves only that title out, with the reason logged."""

    try:
        service = build()
    except ValueError as exc:
        logger.error('%s is not served: %s', name, exc)
        return
    for key in keys:
        services[key] = service


def _monster_hunter_service(
    config: AppConfig,
    storage: StorageBundle,
    profile: MonsterHunterProfile,
    dispatcher: WebhookDispatcher,
    *,
    flows: monsterhunter.AppFlowTracker | None,
) -> monsterhunter.AppService:
    """One Monster Hunter deployment: its own players, Worlds and data folder.

    Only the NA public beta tracks its APP flow (`flows`); retail builds name
    each connection kind with its first request.

    Each announces its Market day and Event quest when the date changes.
    """

    service = monsterhunter.AppService(
        config=monsterhunter.read_app_service_config(config, profile),
        flows=flows,
        directory=read_directory(
            max_players_per_town=config.server.max_players_per_room,
            environment_key=profile.worlds_environment_key,
        ),
        online_players=storage.online_players,
        records=storage.records,
        webhooks=MonsterHunterWebhooks(dispatcher, profile.identifier),
    )
    if dispatcher.enabled:
        DayChangeTimer(service.announce_day).start()
    return service


def _outbreak_service(storage: StorageBundle, *, public_beta: bool = False) -> outbreak.AppService:
    """Resident Evil Outbreak File#1, or its public beta with its own data folder and records."""

    subdirectory = outbreak.PUBLIC_BETA_DATA_SUBDIRECTORY if public_beta else outbreak.DATA_SUBDIRECTORY
    return outbreak.AppService(
        outbreak.read_app_service_config(subdirectory),
        accounts=storage.accounts, records=storage.records, public_beta=public_beta,
    )


def _outbreak2_service(config: AppConfig, storage: StorageBundle) -> outbreak2.AppService:
    """Resident Evil Outbreak File#2: NA and EU share one service, so they share Areas and rooms."""

    return outbreak2.AppService(
        outbreak2.read_app_service_config(config),
        directory=read_outbreak2_directory(),
        accounts=storage.accounts,
        records=storage.records,
        online_players=storage.online_players,
    )
