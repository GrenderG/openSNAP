"""Registry for game-specific web modules."""

from collections.abc import Callable

from opensnap_web.games.automodellista import AutoModellistaWebModule
from opensnap_web.games.monsterhunter import MonsterHunterWebModule
from opensnap_web.games.base import GameWebModule

GameWebModuleFactory = Callable[[], GameWebModule]

GAME_WEB_MODULE_FACTORIES: dict[str, GameWebModuleFactory] = {
    'automodellista': AutoModellistaWebModule,
    'monsterhunter': MonsterHunterWebModule,
}


def list_game_web_modules() -> tuple[str, ...]:
    """List supported game web modules."""

    return tuple(sorted(GAME_WEB_MODULE_FACTORIES))


def create_game_web_module(plugin_name: str) -> GameWebModule:
    """Build game web module by plugin name."""

    normalized = plugin_name.strip().lower()
    factory = GAME_WEB_MODULE_FACTORIES.get(normalized)
    if factory is None:
        supported = ', '.join(list_game_web_modules())
        raise ValueError(
            f'Unsupported web game plugin: {plugin_name}. '
            f'Supported web plugins: {supported}.'
        )
    return factory()
