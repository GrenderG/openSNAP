"""Lobby registry."""

from dataclasses import dataclass

from opensnap.config import LobbyConfig


@dataclass(frozen=True, slots=True)
class Lobby:
    """Lobby metadata."""

    lobby_id: int
    name: str


class LobbyRegistry:
    """In-memory lobby metadata store."""

    def __init__(self, lobbies: tuple[LobbyConfig, ...]) -> None:
        self._lobbies = {
            item.lobby_id: Lobby(lobby_id=item.lobby_id, name=item.name) for item in lobbies
        }

    def list(self) -> list[Lobby]:
        """List lobbies sorted by id."""

        return [self._lobbies[item] for item in sorted(self._lobbies)]

    def get(self, lobby_id: int) -> Lobby | None:
        """Get lobby by id."""

        return self._lobbies.get(lobby_id)
