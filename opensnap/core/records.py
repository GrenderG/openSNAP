"""Game records (lap times, quest clears, hunts) and standings (current values) kept for rankings."""

from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Record:
    """One score a player submitted to a game board.

    `board` names a ranking within the game (an AM course, an MH quest);
    `score` is the board's value (a time ranks lowest first); `details` keeps
    the game-specific fields of the submission.
    """

    record_id: int
    game: str
    board: str
    user_id: int
    player: str
    score: int
    details: Mapping[str, object]
    recorded_at: str


@dataclass(frozen=True, slots=True)
class PlayerTotal:
    """One player's summed score over a set of boards."""

    user_id: int
    player: str
    total: int


@dataclass(frozen=True, slots=True)
class Standing:
    """One player's current value on a game board (a level, a points total).

    Unlike a `Record`, only the latest value is kept. `reached_at` is when the
    player first reported that value: a change resets it, a repeat keeps it,
    so equal values rank by who reached them first.
    """

    game: str
    board: str
    user_id: int
    player: str
    score: int
    reached_at: str
    updated_at: str
