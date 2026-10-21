"""Scenario result reports uploaded by Resident Evil Outbreak after a scenario (APP 6301).

`netwk.bin` (`SLUS_207.65` v2; v1 runs the same code) builds the report at
`0x0059ba40` from the scenario summary that `0x0057bd60` fills when the
client comes back to the lobby:

    u8 cleared, u8 free mode, u8 scenario, field login, u8 cleared, u8 character,
    u32 clear ticks, u32 points, 3 x (field name, u8 status, u8 character),
    u32 echo time, u32 network error, u32 echo replies, u32 echo losses,
    u32 peak queue, u32 peak location, u32 0, u32 0

- `cleared` is 1 when the clear time is nonzero (`0x0057bdd0`); the builder
  sends it twice (`0x0059bae0`).
- `free mode` is 1 in free mode and 0 in scenario mode (`0x008aa2b3`). The
  results screen picks the save's best-time set with it (submain `0x0037c560`;
  offline play uses 0, as scenario mode). After the report a client in
  scenario mode leaves its room and one in free mode keeps it
  (`0x0058439c`). The alpha-server pcaps agree: 1 in every free-mode capture,
  0 in every scenario-mode one.
- `scenario` indexes the scenario names (`opensnap.plugins.outbreak.records`):
  the work field it comes from (`0x005de254`) is loaded from main `0x00316686`
  (`0x005f4ba8`), which demo.bin sets from the current stage through the pair
  table `0x003898c8` (`0x00582700`).
- `login` is the login ID (`0x008a64f0`), the same one as in the 1002 answer.
- `character` codes are described in the records module. The player's own is
  the work fields `+0x1cb0` (character) and `+0x1cb2` (NPC flag, + 8).
- The clear time counts game frames, 30 per second: `game.bin` `0x005feae8`
  adds one per frame, and the saved best time is capped at 9999 h 59 min
  (`0x405f76f8` ticks, submain `0x0037d0bc`). It is 0 without a clear
  (submain `0x0037cda0`).
- `points` are the results screen points (submain `0x0037d630`), which the
  game also adds to its saved point totals (`0x0037cc10`).
- The three teammates are the room's other players (netaq table `0x008a4c00`,
  the own name skipped at `0x0057be94`); an empty name is an empty slot (status
  and character 0). Status is 1 when the results screen found the character
  alive and 2 otherwise: submain `0x0037c5bc` stores `0x0037d9f0` (0 for a
  character that is inactive or in states `0x5e0`, game.bin `0x00666080`,
  unless flag `0x20000000`) at the player's `+2` (netaq `0x008144d0`), and
  `0x0057bf34` reports it as 1/2.
- The echo words are the lobby's 8-echo network test (`0x00583fe0`): total
  round-trip time of the replies, in the same 1/30 s (EE timer 1 hblank count
  x 30 / 14400, `0x005841b0`), then replies and losses.
- `network error` is the number the game shows for an in-game network error
  (main `0x001c7b80`): 850 + an internal code, 890 + an SDK code, 0 if none.
- `peak queue` is the most bytes ever waiting in the in-game message buffer
  `0x003337b0` (main `0x001c6dd0`; overflowing it is error 866 or 871), and
  `peak location` where that happened: stage x 1000 + room (`0x003243da`,
  `0x003243dc`, main `0x001c6e3c`). The summary keeps both as 16 bits.
- The last two words are never written: the summary is zeroed
  (`0x0057bd88`) and nothing stores them before it is copied.

The public beta (`SLUS_290.89`) sends the same report from the same fields
(builder `0x005986a0`, summary `0x0057bd90`), except for the network test: its
first word sums 4 echo round trips in lobby frames (`0x005835e0`), and the
echo replies, echo losses and peak words are never written, so its records
leave them out.

The decoder is shared with File#2 (`opensnap_app.capcom.outbreak.scenario_report`).
"""

from opensnap.core.accounts import Account
from opensnap.plugins.outbreak.records import GAME, clear_board, points_board
from opensnap.storage.interfaces import RecordStore
from opensnap_app.capcom.outbreak.scenario_report import ScenarioResult, Teammate, decode_scenario_result

__all__ = ['ScenarioResult', 'Teammate', 'decode_scenario_result', 'store_scenario_result']


def store_scenario_result(
    records: RecordStore, account: Account, result: ScenarioResult, *, game: str = GAME, network_test: bool = True
) -> None:
    """Keep one report: every run on `points-<n>`, a clear also on `clear-<n>-<mode>`.

    `network_test` keeps the lobby network test words, which the public beta doesn't fill.
    """

    team = [{'name': mate.name, 'alive': mate.alive, 'character': mate.character} for mate in result.teammates]
    details = {
        'free_mode': bool(result.mode),
        'cleared': result.cleared,
        'clear_ticks': result.clear_ticks,
        'character': result.character,
        'teammates': team,
        'network_error': result.network_error,
    }
    if network_test:
        details |= {
            'echo_ticks': result.echo_ticks,
            'echo_replies': result.echo_replies,
            'echo_losses': result.echo_losses,
            'peak_queue': result.peak_queue,
            'peak_location': result.peak_location,
        }
    records.add(
        game=game,
        board=points_board(result.scenario),
        user_id=account.user_id,
        player=account.username,
        score=result.points,
        details=details,
    )
    if result.cleared:
        records.add(
            game=game,
            board=clear_board(result.scenario, bool(result.mode)),
            user_id=account.user_id,
            player=account.username,
            score=result.clear_ticks,
            details={'character': result.character, 'teammates': team},
        )

