"""WSGI entrypoint for production web servers (Apache mod_wsgi, Gunicorn, ...).

Settings come from the environment and `.env` (found next to the repository
when the working directory has none). The listen address, port and TLS belong
to the WSGI server, not to the `OPENSNAP_WEB_*` host/port settings. Relative
paths in `.env` (SQLite file, log path) resolve against the working directory,
so start the server in the repository (mod_wsgi `WSGIDaemonProcess ... home=`,
Gunicorn `--chdir`).
"""

from flask import Flask

from opensnap.env_loader import load_env_file
from opensnap.logging_utils import configure_logging
from opensnap_web.app import create_web_app
from opensnap_web.config import default_web_server_config


def create_wsgi_app() -> Flask:
    """Create WSGI-compatible Flask application instance."""

    load_env_file()
    configure_logging(service_name='web')
    return create_web_app(default_web_server_config())


app = create_wsgi_app()
application = app
