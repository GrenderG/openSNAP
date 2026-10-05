"""Auto Modellista rankings: lap uploads (`/amusa/(am_)up.php`) and the ranking page."""

from collections.abc import Mapping, Sequence
import logging

from opensnap.core.records import Record
from opensnap.storage.interfaces import AccountStore, RecordStore

# Ranking page contract (`/amusa/am_rank.html`, Beta1 `/amusa/rank.html`).
#
# `browser.bin` maps `AM-USA-RANKING` to special-tag type 34 (tag table
# `0x0073b300`), which arms CSV parser mode 8 (`special_tag_check`: mode =
# type - 26). After the `<CSV>` marker, `get_crs_shadow_data(8)`
# (`0x006d911c`, Beta1 `0x008c5b24`) reads rows of four quoted fields:
#
#   "course letter", "name" (15), "team" (15), "time" (7)
#
# - letters `A`..`Q` fill `BsRanking[17][10]` (one board per stage, rows in
#   order of appearance, at most 10); the release also fills the event board
#   `BsRankingSp[50]` from letter `R`. Other letters are skipped. The release
#   reads at most 220 rows, Beta1 at most 170 and has no `R` board.
# - a field ends at the next `"` and longer text is cut, so names and teams
#   cannot contain `"`; a `<` outside quotes ends the rows.
# - `kari_ranking_strings` prints the time as `%c%c'%c%c"%c%c%c`, i.e. the
#   digits `MMSSmmm`, and shows the team under the name.
#
# The client sorts nothing, so each board is sent best first. (The main ELF's
# `Acc_RankRanking` callback is dead code: `Check_RecvData` always returns 0.)
AM_RANK_COURSE_BOARDS = tuple(chr(ord('A') + stage) for stage in range(17))
AM_RANK_COURSE_ROWS = 10
AM_RANK_EVENT_BOARD = 'R'
AM_RANK_EVENT_ROWS = 50
AM_RANK_TEXT_SIZE = 15
AM_RANK_MAX_LAP_MS = 99 * 60_000 + 59_999


def format_am_lap_time(milliseconds: int) -> str:
    """Format one lap time as the ranking page's `MMSSmmm` digits."""

    milliseconds = min(milliseconds, AM_RANK_MAX_LAP_MS)
    minutes, rest = divmod(milliseconds, 60_000)
    seconds, millis = divmod(rest, 1000)
    return f'{minutes:02d}{seconds:02d}{millis:03d}'


def build_am_rank_page(boards: Mapping[str, Sequence[Record]], rows_by_board: Mapping[str, int]) -> str:
    """Build the AM-USA-RANKING page from each board's best records."""

    rows = [
        f'"{board}","{_rank_text(record.player)}","{_rank_text(str(record.details.get("team", "")))}",'
        f'"{format_am_lap_time(record.score)}"'
        for board, limit in rows_by_board.items()
        for record in boards.get(board, ())[:limit]
    ]
    return '\n'.join(
        [
            '<html><head>',
            '<!--AM-USA-RANKING-->',
            '</head>',
            '<!--',
            '<CSV>',
            ',\n'.join(rows),
            '</CSV>',
            '-->',
            '</html>',
            '',
        ]
    )


def _rank_text(text: str) -> str:
    """Keep one name or team inside its quoted field."""

    return ''.join(character for character in text if character not in '"\r\n')[:AM_RANK_TEXT_SIZE]


# Release ranking upload contract (`SLUS_206.42`, `/amusa/am_up.php`),
# recovered from `amus_bin/browser.bin`:
#
# Method and body format:
# - HTTP `POST`
# - `Content-Type: application/x-www-form-urlencoded`
# - key/value separator `=`
# - field separator `&`
# - bytes outside the safe set are percent-escaped as `%HH`
#
# Confirmed emitted fields, in builder order:
# 1) `crs`
#    - source: one-character string at `0x004b5ceb`
#    - upload path is gated by `strlen(source) == 1`
#    - semantic meaning:
#      - confirmed: this is the course/category selector, not a name or
#        time field.
#      - normal case: `'A' + stage_index` from `cw+0x3c08`
#      - release-only special cases:
#        - `'S'` when the club-meeting flag at `cw+0x4a01` is set
#        - `'R'` when `cw+0x4f77 == 3`
#      - confirmed by the ranking UI path: category `3` selects the
#        literal title `Ranking of an event race.` and the special
#        ranking data table, so `R` is the release event-ranking
#        selector.
# 2) `utl`
#    - fixed-width 40-character value assembled as:
#      - `0x004b5cc0`: 15 chars, padded/truncated with ASCII space
#      - `0x004b5cd0`: 15 chars, padded/truncated with ASCII space
#      - `0x004b5ce0`: 10 chars, padded/truncated with ASCII `0`
#    - semantic meaning of those three slices:
#      - `utl[0:15]`: selected account login/user ID. The account-select
#        path copies `tmp_user_id[slot]` into `cw+0x280` with
#        `strncpy(..., 6)`, `cpnLoginLobbyServer` passes that same
#        buffer to `kkLoginClient`, and `lbc_prelogin_02` then copies it
#        into `BsPostUserResult+0x00`. Effective payload: the selected
#        login ID, max 6 significant chars, then space padded to 15.
#      - `utl[15:30]`: selected account team/call-sign text from
#        `tmp_user_team[slot]`, copied via `cw+0x290` into
#        `BsPostUserResult+0x10`. Release also loads
#        `tmp_user_handle[slot]`, but immediately overwrites that same
#        destination with `tmp_user_team[slot]`, so the handle/nickname
#        is confirmed NOT to be uploaded. Effective transmitted width:
#        max 15 significant chars, then space padded to 15.
#      - `utl[30:40]`: normally the selected ranking bucket's best-lap
#        record as a decimal integer in milliseconds. `lbc_prelogin_02`
#        formats the source with `sprintf("%d")`, and
#        `lap_time_disp_sub` later proves the underlying scalar is
#        milliseconds by dividing into `minutes / seconds /
#        milliseconds`. Release has one proven exception: the
#        club-meeting branch (`crs == 'S'`) formats a literal `0`
#        instead of reading a stored lap record.
#    - negative proof from the producer:
#      - no finishing position/rank field is read;
#      - no total race time field is read;
#      - no user handle/nickname field survives into the upload.
# 3) `opt`
#    - source starts at `0x004b5ced`
#    - first 32 bytes are sanitized so any non-hex char becomes `-`
#    - resulting NUL-terminated string is appended as the value
#    - semantic meaning: 4 diagnostic/status words, not leaderboard
#      identity or lap metrics:
#      - `opt[0:8]`: packed performance diagnostics built from
#        `ping_ave_data`, `BsPerformanceTbl`-gated performance bytes,
#        and one browser-state byte
#      - `opt[8:16]`: packed status/error flags built from
#        `NetFuseiFlag`, `game_error_data`, `NET_FLAG`, and one
#        additional state byte
#      - `opt[16:24]`: `kk_return_data`
#      - `opt[24:32]`: `KK_errorno`
#
# Builder limits:
# - field names must stay under 256 bytes
# - aggregate form body must stay under 1024 bytes
#
# Response contract status:
# - confirmed: after submit, the browser enters its async HTTP poll/
#   transaction-completion state machine.
# - unresolved: this RE pass did not prove an upload-specific compare
#   against a literal success body such as `OK` or `00`.
# - safest current assumption is that an HTTP success status plus normal
#   browser transport completion matters more than a specific body text, so
#   the server answers an empty `200 OK`.

# Beta1 ranking/upload delta vs release (`SLUS_204.98`):
#
# Confirmed from `ambeta1_bin/browser.bin` and the main ELF:
# - canonical ranking HTML path is `/amusa/rank.html`
# - canonical ranking upload path is `/amusa/up.php`
# - upload field names and payload construction match release exactly:
#   `crs`, `utl`, `opt`, still emitted as one
#   `application/x-www-form-urlencoded` POST body.
# - Beta1 uses different source buffers for those fields:
#   - `crs` gate/source: `0x0052abbb` (`strlen == 1` required)
#     - semantic meaning: course selector only, always `'A' + stage_index`
#       from `cw+0x4856` in the proven upload producer. Unlike release, the
#       Beta1 producer has no special `R`/`S` event or club-meeting branch.
#   - `utl` slices: `0x0052ab90` (15, space padded),
#     `0x0052aba0` (15, space padded), `0x0052abb0` (10, `0` padded)
#     - `utl[0:15]`: selected account login/user ID. Account selection
#       copies `tmp_user_id[slot]` into `cw+0x280` with `strncpy(..., 6)`,
#       `cpnLoginLobbyServer` passes that buffer to `kkLoginClient`, and
#       `lbc_prelogin_02` copies it into `BsPostUserResult+0x00`. Effective
#       payload: the selected login ID, max 6 significant chars, then space
#       padded to 15 by the browser serializer.
#     - `utl[15:30]`: selected account team/call-sign text from
#       `tmp_user_team[slot]`, copied via `cw+0x29a` into
#       `BsPostUserResult+0x10`. Effective transmitted width: max 15
#       significant chars, then space padded to 15.
#     - `utl[30:40]`: selected board's best-lap record as a decimal integer
#       in milliseconds. Beta1 uses the same `lap_time_disp_sub` logic as
#       release, with divisors proving `mm / ss / ms` formatting.
#     - Beta1 also stores a separate `tmp_user_handle[slot]` at `cw+0x288`,
#       but that handle is confirmed NOT to be uploaded.
#   - `opt` source: `0x0052abbd`, same first-32-byte hex-or-`-`
#     sanitization rule
#     - exact 8-hex chunks:
#       - `opt[0:8]`: `MyPerformance`
#       - `opt[8:16]`: status byte from `cw+0x37dc`
#       - `opt[16:24]`: `kk_return_data`
#       - `opt[24:32]`: `KK_errorno`
# - Negative proof from the producer:
#   - no finishing position/rank is copied;
#   - no total race time is copied;
#   - no user handle/nickname is copied.
# - `AM-USA-RANKING` arms the same CSV parser mode 8 as release
#   (`get_crs_shadow_data` `0x008c5b24`), but only for letters `A`..`Q`
#   and at most 170 rows: Beta1 has no event board.
# - the exact upload success body, if any, is unresolved, as for release.


class AutoModellistaRankings:
    """Best-lap boards fed by `/amusa/(am_)up.php` and shown by the ranking page."""

    def __init__(
        self,
        *,
        game: str,
        accounts: AccountStore,
        records: RecordStore,
        rows_by_board: Mapping[str, int],
    ) -> None:
        self._game = game
        self._accounts = accounts
        self._records = records
        self._rows_by_board = rows_by_board
        self._logger = logging.getLogger('opensnap.web.rankings')

    def submit(self, course: str, utl: str) -> None:
        """Keep one upload's best lap on its course board.

        Only boards the ranking page shows are kept: the club meeting upload
        (`S`) carries a literal 0 instead of a lap, and 0 is never a lap time.
        The login ID must name an existing account.
        """

        lap = utl[30:40]
        if course not in self._rows_by_board or len(utl) != 40 or not lap.isdigit() or int(lap) == 0:
            self._logger.info('Ignoring ranking upload crs=%r utl=%r.', course, utl)
            return
        account = self._accounts.get_by_name(utl[:15].rstrip(' '))
        if account is None:
            self._logger.warning('Ignoring ranking upload for unknown login ID %r.', utl[:15])
            return
        self._records.add(
            game=self._game,
            board=course,
            user_id=account.user_id,
            player=account.username,
            score=int(lap),
            details={'team': utl[15:30].rstrip(' ')},
        )
        self._logger.info('Stored %s lap %s ms on course %s.', account.username, int(lap), course)

    def page(self) -> str:
        """Render the current boards."""

        limit = max(self._rows_by_board.values())
        return build_am_rank_page(self._records.best_by_board(self._game, limit), self._rows_by_board)
