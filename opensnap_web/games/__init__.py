"""Game-specific web route modules."""

from opensnap_web.games.registry import create_game_web_module, list_game_web_modules

__all__ = ['create_game_web_module', 'list_game_web_modules']
