"""Command routing."""

from collections.abc import Callable

from opensnap.protocol.models import SnapMessage

Handler = Callable[['HandlerContext', SnapMessage], list[SnapMessage]]


class CommandRouter:
    """Maps command ids to handlers."""

    def __init__(self) -> None:
        self._handlers: dict[int, Handler] = {}

    def register(self, command: int, handler: Handler) -> None:
        """Register command handler."""

        self._handlers[command] = handler

    def has_handler(self, command: int) -> bool:
        """Report whether a command has a registered handler."""

        return command in self._handlers

    def dispatch(self, context: 'HandlerContext', message: SnapMessage) -> list[SnapMessage]:
        """Dispatch a message to matching command handler."""

        handler = self._handlers.get(message.command)
        if handler is None:
            return []
        return handler(context, message)


# Import at file end to avoid circular import during type checking.
from opensnap.core.context import HandlerContext  # noqa: E402  pylint: disable=wrong-import-position
