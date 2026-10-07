"""Players logged in to a game server, shared with companion services such as Capcom APP."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class OnlinePlayer:
    """One logged-in player as its game server publishes it.

    `login_serial` changes on every game login (MH repeats KICS on each Land
    entry), so a reader can tell a new login from one it already saw.
    `area_id` is the player's lobby (0 = none).
    """

    session_id: int
    user_id: int
    username: str
    host: str
    area_id: int
    login_serial: int
