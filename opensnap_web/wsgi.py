"""WSGI entrypoint for production web servers."""

from flask import Flask

from opensnap.env_loader import load_env_file
from opensnap_web.app import create_web_app
from opensnap_web.config import default_web_server_config


def create_wsgi_app() -> Flask:
    """Create WSGI-compatible Flask application instance."""

    load_env_file()
    return create_web_app(default_web_server_config())


app = create_wsgi_app()
application = app
