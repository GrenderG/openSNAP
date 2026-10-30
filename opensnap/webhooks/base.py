"""Base class for one game's webhook announcements."""

from opensnap.webhooks.dispatcher import WebhookDispatcher, WebhookEvent


class GameWebhooks:
    """Announcements every game shares; each game extends it with its own.

    Subclasses add one method per announcement that builds the text, so call
    sites pass plain values and never format messages themselves.
    """

    def __init__(self, dispatcher: WebhookDispatcher, game: str) -> None:
        self._dispatcher = dispatcher
        self.game = game

    def emit(self, kind: str, content: str, **data: object) -> None:
        """Queue one announcement of this game."""

        self._dispatcher.send(WebhookEvent(game=self.game, kind=kind, content=content, data=data))

    def server_started(self) -> None:
        self.emit('server_started', f'>>> *The {self.game} server has been started.*')
