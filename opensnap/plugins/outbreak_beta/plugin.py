"""Resident Evil Outbreak NA public beta (`SLUS_290.89`, December 2003).

The beta runs the release's SN@P SDK unchanged (the same Dec 3 2003 build,
main ELF addresses = `SLUS_207.65` v2 `- 0x2c90`) under an earlier lobby
(`netwk.bin` at `0x00570000`, compared function by function with the
release's). It differs from the release plugin in:

- its lobby status `0xf7c00001` (login `0x00583cd8`, back from a scenario
  `0x005837a4`; the release sends `0xf7e00001`);
- its Areas (`directory.read_public_beta_directory`), and free mode first
  joining lobby 0, which must succeed (`0x00585afc`, callback `0x005859d0`);
- its room list: the row index restarts on every reply and the request ends
  with the first one (`0x00587b80`), so it gets one reply, which holds 24 rows;
- its return from a scenario: it leaves its room and Area (`0x0058381c`,
  `0x005837cc`) before its lobby status.

Slot 19 (reliable send-target, `0x00582fa0`) stays registered into `game.bin`
during a scenario (main `0x001c9330` registers only slots 18 and 20), so the
release rule of not sending lobby callbacks to a player in a scenario keeps
the beta from crashing.

It logs in like the release (title code, footer, bootstrap host
`snap01.reo.capcom.sf.yav4.com`), so a bootstrap that accepts this game (and
not `outbreak`) routes it here.
"""

import logging

from opensnap.core.context import HandlerContext
from opensnap.core.sessions import Session
from opensnap.plugins.base import SnapTitle
from opensnap.plugins.common import resolve_session
from opensnap.plugins.outbreak.directory import Directory, read_public_beta_directory
from opensnap.plugins.outbreak.plugin import RESULT_OK, TITLE_CODE, OutbreakPlugin
from opensnap.protocol.constants import FLAG_CHANNEL_BITS, FOOTER_MARKER
from opensnap.protocol.fields import get_u32
from opensnap.protocol.models import SnapMessage

_LOGGER = logging.getLogger('opensnap.plugins.outbreak_beta')

STATUS_IN_LOBBY = 0xF7C00001
# One room list reply: (876 - 12-byte header) // 36-byte rows.
MAX_LISTED_ROOMS = 24
# Free mode's lobby id before an Area is chosen.
NO_AREA = 0


class OutbreakBetaPlugin(OutbreakPlugin):
    """The release plugin with the beta's lobby status, Areas and room list."""

    name = 'outbreak_beta'
    # `kkLoginClient` call `0x00583d28`: the release's title code and footer.
    snap_titles = (
        SnapTitle(title_code=TITLE_CODE, footer_marker=FOOTER_MARKER, name='Resident Evil Outbreak NA public beta'),
    )
    lobby_status = STATUS_IN_LOBBY
    max_listed_rooms = MAX_LISTED_ROOMS

    def _read_directory(self) -> Directory:
        return read_public_beta_directory()

    def _handle_join(self, context: HandlerContext, message: SnapMessage) -> list[SnapMessage]:
        """Joining lobby 0 leaves the player outside the Areas."""

        if message.type_flags & FLAG_CHANNEL_BITS != FLAG_CHANNEL_BITS or get_u32(message.payload, 0) != NO_AREA:
            return super()._handle_join(context, message)
        session = resolve_session(context, message, _LOGGER)
        if session is None:
            return []
        departures = self._leave_room(context, session)
        context.sessions.set_lobby(session.session_id, NO_AREA)
        return departures + [self._result(context, message, session, RESULT_OK, FLAG_CHANNEL_BITS)]

    def _leave_room(self, context: HandlerContext, session: Session, *, notify: bool = True) -> list[SnapMessage]:
        """Also drop the leaves held back for the player: they are about the room it is leaving."""

        player = self._players.get(session.session_id)
        if player is not None and session.room_id > 0:
            player.pending_leaves.clear()
        return super()._leave_room(context, session, notify=notify)
