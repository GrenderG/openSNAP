"""Game records (lap times, quest clears, hunts) kept for rankings."""

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
