"""Scenario result reports uploaded by Resident Evil Outbreak File#2 (APP 6301).

The report has File#1's layout (`opensnap_app.capcom.scenario_report`). The
File#2 client builds it at NA `0x001fd530` (EU `0x001c8b60`) from the
156-byte scenario summary `0x0064dfc4` that `netwk.bin` fills on the return
from a scenario (`0x005938d0`); `tasks/re_outbreak2_results.md` traces each
field to the game: File#1's free-mode byte is the room rule "rank kind", a
ranking category the server defines per Area (`opensnap.plugins.outbreak2.records`),
and the two words File#1 left at zero are the peak of a one-second message
counter (`0x001f2384`) and a `game.bin` flag word (`0x00300770`), kept with
the other network diagnostics. The client zeroes the clear, its time and its
points when Friendly Fire, NIGHTMARE, INFINITY or a Point Multiplier is on
(`0x005938d0`), so those runs reach the points board with 0 points only.
"""

from opensnap.core.accounts import Account
from opensnap.plugins.outbreak2.records import GAME, clear_board, points_board
from opensnap.storage.interfaces import RecordStore
from opensnap_app.capcom.scenario_report import ScenarioResult


def store_scenario_result(records: RecordStore, account: Account, result: ScenarioResult) -> None:
    """Keep one report: every run on `points-<n>`, a clear also on `clear-<n>-<rank kind>`."""

    team = [{'name': mate.name, 'alive': mate.alive, 'character': mate.character} for mate in result.teammates]
    records.add(
        game=GAME,
        board=points_board(result.scenario),
        user_id=account.user_id,
        player=account.username,
        score=result.points,
        details={
            'rank_kind': result.mode,
            'cleared': result.cleared,
            'clear_ticks': result.clear_ticks,
            'character': result.character,
            'teammates': team,
            'echo_ticks': result.echo_ticks,
            'echo_replies': result.echo_replies,
            'echo_losses': result.echo_losses,
            'network_error': result.network_error,
            'peak_queue': result.peak_queue,
            'peak_location': result.peak_location,
            'peak_messages': result.peak_messages,
            'flags': result.flags,
        },
    )
    if result.cleared:
        records.add(
            game=GAME,
            board=clear_board(result.scenario, result.mode),
            user_id=account.user_id,
            player=account.username,
            score=result.clear_ticks,
            details={'character': result.character, 'teammates': team},
        )
