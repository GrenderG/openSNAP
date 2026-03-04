"""Configuration for the web service."""

from dataclasses import dataclass
import os

from opensnap.env_loader import load_env_file


@dataclass(frozen=True, slots=True)
class WebServerConfig:
    """Runtime settings for the web service."""

    host: str = '0.0.0.0'
    port: int = 80
    https_host: str = '0.0.0.0'
    https_port: int = 443
    https_certfile: str = ''
    https_keyfile: str = ''
    game_plugin: str = 'automodellista'

    @property
    def https_enabled(self) -> bool:
        """Return whether the HTTPS listener has a complete TLS configuration."""

        return bool(self.https_certfile and self.https_keyfile)


def default_web_server_config() -> WebServerConfig:
    """Build web configuration from environment variables."""

    load_env_file()
    host = os.getenv('OPENSNAP_WEB_HOST', '0.0.0.0').strip() or '0.0.0.0'
    port_value = os.getenv('OPENSNAP_WEB_PORT', '80').strip()
    https_host = os.getenv('OPENSNAP_WEB_HTTPS_HOST', host).strip() or host
    https_port_value = os.getenv('OPENSNAP_WEB_HTTPS_PORT', '443').strip()
    https_certfile = os.getenv('OPENSNAP_WEB_HTTPS_CERTFILE', '').strip()
    https_keyfile = os.getenv('OPENSNAP_WEB_HTTPS_KEYFILE', '').strip()
    game_plugin = os.getenv(
        'OPENSNAP_WEB_GAME_PLUGIN',
        os.getenv('OPENSNAP_GAME_PLUGIN', 'automodellista'),
    ).strip().lower()

    try:
        port = int(port_value)
    except ValueError:
        port = 80

    try:
        https_port = int(https_port_value)
    except ValueError:
        https_port = 443

    return WebServerConfig(
        host=host,
        port=port,
        https_host=https_host,
        https_port=https_port,
        https_certfile=https_certfile,
        https_keyfile=https_keyfile,
        game_plugin=game_plugin,
    )
