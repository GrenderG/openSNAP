"""Plugin registry and resolution helpers."""

from collections.abc import Callable

from opensnap.plugins.automodellista import AutoModellistaPlugin
from opensnap.plugins.automodellista_beta1 import AutoModellistaBeta1Plugin
from opensnap.plugins.base import GamePlugin

PluginFactory = Callable[[], GamePlugin]

PLUGIN_FACTORIES: dict[str, PluginFactory] = {
    'automodellista': AutoModellistaPlugin,
    'automodellista_beta1': AutoModellistaBeta1Plugin,
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
