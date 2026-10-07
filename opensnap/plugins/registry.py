"""Plugin registry and resolution helpers."""

from opensnap.plugins.automodellista import AutoModellistaPlugin
from opensnap.plugins.automodellista_beta1 import AutoModellistaBeta1Plugin
from opensnap.plugins.base import GamePlugin
from opensnap.plugins.monsterhunter import MonsterHunterPlugin
from opensnap.plugins.monsterhunter_na_beta import MonsterHunterNaBetaPlugin
from opensnap.plugins.outbreak import OutbreakPlugin

PLUGIN_FACTORIES: dict[str, type[GamePlugin]] = {
    'automodellista': AutoModellistaPlugin,
    'automodellista_beta1': AutoModellistaBeta1Plugin,
    'monsterhunter': MonsterHunterPlugin,
    'monsterhunter_na_beta': MonsterHunterNaBetaPlugin,
    'outbreak': OutbreakPlugin,
}


def list_game_plugins() -> tuple[str, ...]:
    """List supported plugin names."""

    return tuple(sorted(PLUGIN_FACTORIES))


def create_game_plugin(plugin_name: str) -> GamePlugin:
    """Build plugin instance by configured name."""

    normalized = plugin_name.strip().lower()
    factory = PLUGIN_FACTORIES.get(normalized)
    if factory is None:
        supported = ', '.join(list_game_plugins())
        raise ValueError(
            f'Unsupported game plugin: {plugin_name}. '
            f'Supported plugins: {supported}.'
        )
    return factory()


def identify_snap_title(
    title_code: int, footer_marker: int, games: tuple[str, ...] = ()
) -> tuple[str, str] | None:
    """Return `(plugin name, title name)` for one bootstrap login's title code and footer.

    Only `games` are considered when given (`OPENSNAP_BOOTSTRAP_GAMES`): builds
    that share a title code, like the Monster Hunter release and its NA public
    beta, are told apart by which bootstrap they log in to. Otherwise the first
    plugin in registry order wins.
    """

    for plugin_name, plugin_class in PLUGIN_FACTORIES.items():
        if games and plugin_name not in games:
            continue
        for title in plugin_class.snap_titles:
            if (title.title_code, title.footer_marker) == (title_code, footer_marker):
                return plugin_name, title.name
    return None
