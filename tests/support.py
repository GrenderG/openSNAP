"""Shared test helpers."""

from dataclasses import replace
import struct

from opensnap.config import AppConfig, GameServerTargetConfig
from opensnap.storage.factory import create_storage
from opensnap.webhooks import WebhookDispatcher, WebhookEvent

# SN@P title codes passed to `kkLoginClient` (see `opensnap.plugins` `snap_titles`).
AUTO_MODELLISTA_TITLE_CODE = 0xCAAD
MONSTER_HUNTER_NA_TITLE_CODE = 0xCA03


def login_client_payload(login: bytes, title_code: int = AUTO_MODELLISTA_TITLE_CODE) -> bytes:
    """`kkLoginClient` payload: login field (40), auth string (60), title code, zero, address, ports."""

    return login.ljust(40, b'\x00') + login + b'@cei-auth'.ljust(60 - len(login), b'\x00') + struct.pack(
        '>5L', title_code, 0, 0xC0000264, 2000, 1000
    )


def serving(config: AppConfig, game: str) -> AppConfig:
    """Pin the game a test server serves, independent of the local `.env`."""

    server = config.server
    target = GameServerTargetConfig(
        game_identifier=game,
        host=server.game.advertise_host or server.game.host,
        port=server.game.port,
    )
    return replace(
        config,
        server=replace(server, game_plugin=game, game_targets=(target,)),
    )


def create_accounts(config: AppConfig, *usernames: str, password: str = '1111', kage: bool = False) -> None:
    """Create accounts in the configured shared store, as the signup page does.

    `kage` also stores the KAGE login key, as a KAGE-SDK game's signup page does.
    """

    storage = create_storage(config)
    try:
        for username in usernames:
            account = storage.accounts.create_user(username, password)
            if kage:
                storage.accounts.set_kage_login_key(account, password.encode('utf-8'))
    finally:
        storage.close()


class RecordingDispatcher(WebhookDispatcher):
    """Keep sent webhook events instead of posting them."""

    def __init__(self) -> None:
        super().__init__()
        self.events: list[WebhookEvent] = []

    def send(self, event: WebhookEvent) -> None:
        self.events.append(event)
