"""Game plugin exports and registry helpers."""

from opensnap.plugins.automodellista import AutoModellistaPlugin
from opensnap.plugins.registry import create_game_plugin, list_game_plugins

__all__ = ['AutoModellistaPlugin', 'create_game_plugin', 'list_game_plugins']
