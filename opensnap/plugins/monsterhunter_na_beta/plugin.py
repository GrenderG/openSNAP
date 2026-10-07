"""Monster Hunter NA public beta (`SLUS_291.10`, May 2004).

The beta runs the release's lobby and quest protocol with two differences
(`lobby.bin` compared function by function with `SLUS_208.96`):

- Its member profile is 216 bytes with the departure state at `+213/+215`
  (`MONSTER_HUNTER_NA_BETA`). Each client copies its own profile size from
  roster records, so beta and release players can't share a server.
- It has no lobby keepalive: the release's echo every 6213 word-6 frames
  (`0x0061d440` -> `0x00613540`) has no counterpart, and its 6213 reply
  holds four words. An idle beta player in an Area sends nothing.

It logs in with the release's title code, so a bootstrap that accepts this
game (and not `monsterhunter`) routes it here: the beta's own bootstrap host
is `snap01.reo.capcom.sf.yav4.com` (Outbreak's), the release's
`bootstrap01.mh-beta.capcom.sf.yav4.com`.
"""

from opensnap.core.context import HandlerContext
from opensnap.core.sessions import Session
from opensnap.plugins.base import SnapTitle
from opensnap.plugins.monsterhunter.plugin import QUEST_IDLE_LIMIT_SECONDS, MonsterHunterPlugin
from opensnap.plugins.monsterhunter.profile import MONSTER_HUNTER_NA_BETA
from opensnap.protocol.constants import FOOTER_MARKER


class MonsterHunterNaBetaPlugin(MonsterHunterPlugin):
    """The release plugin with the beta's profile layout and idle rule."""

    name = MONSTER_HUNTER_NA_BETA.identifier
    # `kkLoginClient` call `0x005bbbf4`: the release's title code and footer.
    snap_titles = (SnapTitle(title_code=0xCA03, footer_marker=FOOTER_MARKER, name='Monster Hunter NA public beta'),)

    def __init__(self) -> None:
        super().__init__(MONSTER_HUNTER_NA_BETA)

    def session_idle_limit(self, context: HandlerContext, session: Session) -> float | None:
        """Only a started quest has an idle limit: the beta lobby sends no keepalive."""

        return QUEST_IDLE_LIMIT_SECONDS if self._in_started_quest(context, session) else None
