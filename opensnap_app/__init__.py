"""Companion services some games use beside SN@P, one package per service.

Each runs as its own process: `python run.py app <name>`.
"""

from importlib import import_module

APPS = ('capcom',)


def run_app(name: str) -> None:
    """Run one companion service's process."""

    if name not in APPS:
        raise SystemExit(f'Unknown app {name!r}; available: {", ".join(APPS)}.')
    import_module(f'opensnap_app.{name}.main').main()
