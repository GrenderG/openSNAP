"""Scenario result reports uploaded by Resident Evil Outbreak File#2 (APP 6301).

The report has File#1's layout
(`opensnap_app.capcom.outbreak.scenario_report`). The File#2 client builds it
at NA `0x001fd530` (EU `0x001c8b60`) from the 156-byte scenario summary
`0x0064dfc4` that `netwk.bin` fills on the return from a scenario
(`0x005938d0`). Traced to the game, File#1's free-mode byte is the room rule
"rank kind", a ranking category the server defines per Area
(`opensnap.plugins.outbreak2.records`), and the two words File#1 left at zero
are the peak of a one-second message counter (`0x001f2384`) and a `game.bin`
flag word (`0x00300770`), network diagnostics like the others. The client
zeroes the clear, its time and its points when Friendly Fire, NIGHTMARE,
INFINITY or a Point Multiplier is on (`0x005938d0`), so those runs reach the
points board with 0 points only.
"""

from opensnap.core.accounts import Account
from opensnap.core.records import Ranking
from opensnap.plugins.outbreak2.records import GAME, POINTS_BOARD, clear_board
from opensnap.storage.interfaces import RecordStore
from opensnap_app.capcom.outbreak.scenario_report import ScenarioResult


def store_scenario_result(records: RecordStore, account: Account, result: ScenarioResult) -> None:
    """Keep one report: its points add to the player's `points` total, a clear goes on `clear-<n>-<rank kind>`.

    A clear time is kept when it is the player's fastest on that board.
    """

    records.submit(
        game=GAME,
        board=POINTS_BOARD,
        ranking=Ranking.TOTAL,
        user_id=account.user_id,
        player=account.username,
        score=result.points,
    )
    if result.cleared:
        team = [{'name': mate.name, 'alive': mate.alive, 'character': mate.character} for mate in result.teammates]
        records.submit(
            game=GAME,
            board=clear_board(result.scenario, result.mode),
            ranking=Ranking.LOWEST,
            user_id=account.user_id,
            player=account.username,
            score=result.clear_ticks,
            details={'character': result.character, 'teammates': team},
        )
