"""Shared test helpers."""

from dataclasses import replace
import struct

from opensnap.config import AppConfig, GameServerTargetConfig

# SN@P title codes passed to `kkLoginClient` (see `opensnap.plugins` `snap_titles`).
AUTO_MODELLISTA_TITLE_CODE = 0xCAAD
MONSTER_HUNTER_NA_TITLE_CODE = 0xCA03


def login_client_payload(login: bytes, title_code: int = AUTO_MODELLISTA_TITLE_CODE) -> bytes:
    """`kkLoginClient` payload: login field (40), auth string (60), title code, zero, address, ports."""

    return login.ljust(40, b'\x00') + b'test\n@cei-auth'.ljust(60, b'\x00') + struct.pack(
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
