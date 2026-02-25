"""Configuration for the web bootstrap service."""

from dataclasses import dataclass
import os

from opensnap.env_loader import load_env_file


@dataclass(frozen=True, slots=True)
class WebServerConfig:
    """Runtime settings for the web service."""

    host: str = '0.0.0.0'
    port: int = 80
    game_plugin: str = 'automodellista'


def default_web_server_config() -> WebServerConfig:
    """Build web configuration from environment variables."""

    load_env_file()
    host = os.getenv('OPENSNAP_WEB_HOST', '0.0.0.0').strip() or '0.0.0.0'
    port_value = os.getenv('OPENSNAP_WEB_PORT', '80').strip()
    game_plugin = os.getenv(
        'OPENSNAP_WEB_GAME_PLUGIN',
        os.getenv('OPENSNAP_GAME_PLUGIN', 'automodellista'),
    ).strip().lower()

    try:
        port = int(port_value)
    except ValueError:
        port = 80

    return WebServerConfig(
        host=host,
        port=port,
        game_plugin=game_plugin,
    )
