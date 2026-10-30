"""Contract for game web modules."""

from typing import Protocol

from flask import Blueprint

from opensnap.storage.interfaces import StorageBundle
from opensnap.webhooks import WebhookDispatcher


class GameWebModule(Protocol):
    """One game's web routes, built as a Flask blueprint over the shared store."""

    name: str

    def blueprint(self, storage: StorageBundle, dispatcher: WebhookDispatcher) -> Blueprint:
        """Return the game's routes; announcements go through `dispatcher`."""
