"""Game records (lap times, quest clears, kill totals, points): one per player and board, kept for rankings."""

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum


class Ranking(Enum):
    """How a board ranks its records, and so how a submission changes the player's record."""

    # The latest value replaces the record (a level or points total that can drop); highest first.
    LATEST = 'latest'
    # The record keeps the player's lowest submission (a time); lowest first.
    LOWEST = 'lowest'
    # The record keeps the player's highest submission; highest first.
    HIGHEST = 'highest'
    # Submissions add up (kills, points); highest first.
    TOTAL = 'total'


@dataclass(frozen=True, slots=True)
class Record:
    """One player's record on a game board.

    `board` names a ranking within the game (an AM course, an MH quest);
    `score` is the board's value; `details` keeps the game-specific fields of
    the submission that set it. `reached_at` is when the score was first
    reached: a change resets it, a repeat keeps it, so equal scores rank by
    who reached them first.
    """

    game: str
    board: str
    user_id: int
    player: str
    score: int
    details: Mapping[str, object]
    reached_at: str


@dataclass(frozen=True, slots=True)
class RankedRecord:
    """A record with its rank on the board (1 = best)."""

    rank: int
    record: Record
