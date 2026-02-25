"""Plugin contracts."""

from typing import Protocol

from opensnap.core.context import HandlerContext
from opensnap.core.router import CommandRouter
from opensnap.protocol.models import SnapMessage


class GamePlugin(Protocol):
    """Game plugin interface."""

    name: str

    def register_handlers(self, router: CommandRouter, context: HandlerContext) -> None:
        """Register game-specific command handlers."""

    def on_tick(self, context: HandlerContext) -> list[SnapMessage]:
        """Run periodic plugin work."""
