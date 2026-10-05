"""Plugin registry and resolution helpers."""

from opensnap.plugins.automodellista import AutoModellistaPlugin
from opensnap.plugins.automodellista_beta1 import AutoModellistaBeta1Plugin
from opensnap.plugins.base import GamePlugin
from opensnap.plugins.monsterhunter import MonsterHunterPlugin

PLUGIN_FACTORIES: dict[str, type[GamePlugin]] = {
    'automodellista': AutoModellistaPlugin,
    'automodellista_beta1': AutoModellistaBeta1Plugin,
    'monsterhunter': MonsterHunterPlugin,
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


def identify_snap_title(title_code: int, footer_marker: int) -> tuple[str, str] | None:
    """Return `(plugin name, title name)` for one bootstrap login's title code and footer."""

    for plugin_name, plugin_class in PLUGIN_FACTORIES.items():
        for title in plugin_class.snap_titles:
            if (title.title_code, title.footer_marker) == (title_code, footer_marker):
                return plugin_name, title.name
    return None
