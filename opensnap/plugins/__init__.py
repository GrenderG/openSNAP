"""Game plugin exports and registry helpers."""

from opensnap.plugins.automodellista import AutoModellistaPlugin
from opensnap.plugins.automodellista_beta1 import AutoModellistaBeta1Plugin
from opensnap.plugins.registry import create_game_plugin, list_game_plugins

__all__ = [
    'AutoModellistaPlugin',
    'AutoModellistaBeta1Plugin',
    'create_game_plugin',
    'list_game_plugins',
]
