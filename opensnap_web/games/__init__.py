"""Game web modules and the `OPENSNAP_WEB_GAME_PLUGIN` selection."""

from opensnap_web.games.automodellista import AutoModellistaBeta1WebModule, AutoModellistaWebModule
from opensnap_web.games.base import GameWebModule
from opensnap_web.games.monsterhunter import MonsterHunterWebModule

GENERIC = 'generic'
# Registration order for `generic`. Paths served by several modules (signup,
# patch pages) go to the first one, so Beta1 stays ahead of release.
WEB_MODULES: dict[str, GameWebModule] = {
    module.name: module
    for module in (AutoModellistaBeta1WebModule(), AutoModellistaWebModule(), MonsterHunterWebModule())
}


def select_web_modules(selection: str) -> tuple[GameWebModule, ...]:
    """Return every module for `generic`, otherwise the named one."""

    name = selection.strip().lower()
    if name == GENERIC:
        return tuple(WEB_MODULES.values())
    if name not in WEB_MODULES:
        raise ValueError(
            f'Unsupported web game plugin: {selection}. Supported: {GENERIC}, {", ".join(sorted(WEB_MODULES))}.'
        )
    return (WEB_MODULES[name],)
