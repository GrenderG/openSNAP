"""Resident Evil Outbreak File#2 ranking boards for `opensnap.plugins.outbreak.scenario_rankings`."""

from opensnap.plugins.outbreak2.directory import Directory
from opensnap.plugins.outbreak2.records import (
    CLEAR_TICKS_PER_SECOND,
    GAME,
    POINTS_BOARD_PREFIX,
    SCENARIO_NAMES,
    character_name,
    clear_board,
)
from opensnap.plugins.outbreak.scenario_rankings import ScenarioBoards


def boards(directory: Directory) -> ScenarioBoards:
    """One clear board per rank kind the server's Areas assign, labelled as they are."""

    rank_kinds = directory.rank_kind_names()

    def clear_boards(scenario: int) -> tuple[tuple[str, str], ...]:
        return tuple(
            (f'{SCENARIO_NAMES[scenario]}: {label}', clear_board(scenario, rank_kind))
            for rank_kind, label in sorted(rank_kinds.items())
        )

    return ScenarioBoards(
        game=GAME,
        scenario_names=SCENARIO_NAMES,
        clear_boards=clear_boards,
        points_board_prefix=POINTS_BOARD_PREFIX,
        character_name=character_name,
        clear_ticks_per_second=CLEAR_TICKS_PER_SECOND,
        hide_empty_boards=True,
    )
